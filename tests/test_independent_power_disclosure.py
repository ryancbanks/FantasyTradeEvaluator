import copy
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import json
import unittest

from tests.test_strength import make_model, player
from trade_snapshot.independent_power_disclosure import (
    INDEPENDENT_POWER_MODE,
    INDEPENDENT_POWER_NOTICE,
    INDEPENDENT_POWER_STATUS,
    IndependentPowerDisclosure,
)


CAPTURED_AT = datetime(2026, 9, 2, 12, 30, tzinfo=timezone.utc)


def model(*, residual=1, denominator=3, **changes):
    return make_model(
        ("QB",),
        (player("qb", residual, {"QB": 2}),),
        denominator,
        **changes,
    )


def disclosure(strength_model=None, **changes):
    values = {
        "policy_id": "independent-power-v1",
        "provider_names": ("yahoo", "espn"),
        "captured_at": CAPTURED_AT,
    }
    values.update(changes)
    return IndependentPowerDisclosure.from_strength_model(
        strength_model or model(), **values
    )


class IndependentPowerDisclosureTests(unittest.TestCase):
    def test_binds_normalized_provenance_to_strength_model(self):
        strength_model = model()
        value = disclosure(
            strength_model,
            captured_at=datetime(
                2026, 9, 2, 7, 30, tzinfo=timezone(timedelta(hours=-5))
            ),
        )

        self.assertEqual(value.weekly_snapshot_id, strength_model.snapshot_id)
        self.assertEqual(value.strength_model_id, strength_model.model_id)
        self.assertEqual(value.provider_names, ("espn", "yahoo"))
        self.assertEqual(value.captured_at, CAPTURED_AT)
        self.assertEqual(value.mode, INDEPENDENT_POWER_MODE)
        self.assertEqual(value.status, INDEPENDENT_POWER_STATUS)
        self.assertEqual(value.notice, INDEPENDENT_POWER_NOTICE)
        self.assertIn("not a FantasyPros score", value.notice)
        self.assertIn("no output", value.notice)
        self.assertEqual(value.current_evidence_id, value.disclosure_id)
        self.assertEqual(value.current_evidence_at, CAPTURED_AT)
        self.assertEqual(value.current_holdout_count, 0)
        self.assertEqual(value.validated_balanced_package_sizes, ())
        value.validate_bundle(
            snapshot_id=strength_model.snapshot_id,
            strength_model=strength_model,
        )

    def test_every_valid_trade_shape_has_only_independent_status(self):
        value = disclosure()

        for outgoing, incoming, adjusted in (
            (1, 1, False),
            (4, 4, False),
            (1, 3, True),
            (20, 19, False),
        ):
            with self.subTest(
                outgoing=outgoing, incoming=incoming, adjusted=adjusted
            ):
                self.assertEqual(
                    value.power_result_status(
                        outgoing_count=outgoing,
                        incoming_count=incoming,
                        has_roster_adjustment=adjusted,
                    ),
                    INDEPENDENT_POWER_STATUS,
                )

        for arguments in (
            (0, 1, False),
            (1, 0, False),
            (True, 1, False),
            (1, 1, 1),
        ):
            with self.assertRaisesRegex(ValueError, "trade shape"):
                value.power_result_status(
                    outgoing_count=arguments[0],
                    incoming_count=arguments[1],
                    has_roster_adjustment=arguments[2],
                )

    def test_strict_round_trip_and_content_tamper_detection(self):
        value = disclosure()
        record = value.to_record()

        json.dumps(record, allow_nan=False)
        self.assertEqual(
            IndependentPowerDisclosure.from_record(record),
            value,
        )

        for field, replacement in (
            ("policy_id", "changed-policy"),
            ("provider_names", ["espn"]),
            ("captured_at", "2026-09-03T12:30:00.000000Z"),
        ):
            tampered = copy.deepcopy(record)
            tampered[field] = replacement
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "disclosure_id"
            ):
                IndependentPowerDisclosure.from_record(tampered)

        for field, replacement in (
            ("kind", "other"),
            ("schema_version", 2),
            ("mode", "exact"),
            ("status", "exact"),
            ("notice", "exact"),
        ):
            tampered = copy.deepcopy(record)
            tampered[field] = replacement
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "schema"
            ):
                IndependentPowerDisclosure.from_record(tampered)

        extra = copy.deepcopy(record)
        extra["extra"] = True
        with self.assertRaisesRegex(ValueError, "fields"):
            IndependentPowerDisclosure.from_record(extra)

    def test_content_id_changes_with_each_provenance_dimension(self):
        base = disclosure()
        changed = (
            disclosure(policy_id="independent-power-v2"),
            disclosure(provider_names=("espn",)),
            disclosure(captured_at=CAPTURED_AT + timedelta(seconds=1)),
            disclosure(model(snapshot_id="snapshot-2")),
        )

        self.assertEqual(len({base.disclosure_id, *(row.disclosure_id for row in changed)}), 5)

    def test_rejects_invalid_or_fantasypros_provenance(self):
        for providers in (
            (),
            ("espn", "espn"),
            ("ESPN",),
            ("fantasypros", "espn"),
            ("fantasypros_projection", "espn"),
            "espn",
        ):
            with self.subTest(providers=providers), self.assertRaisesRegex(
                ValueError, "provider"
            ):
                disclosure(provider_names=providers)

        with self.assertRaisesRegex(ValueError, "policy_id"):
            disclosure(policy_id=" ")
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            disclosure(captured_at=datetime(2026, 9, 2))
        with self.assertRaisesRegex(ValueError, "StrengthModel"):
            IndependentPowerDisclosure.from_strength_model(
                object(),
                policy_id="independent-power-v1",
                provider_names=("espn",),
                captured_at=CAPTURED_AT,
            )

    def test_validation_rejects_detached_snapshot_or_model(self):
        strength_model = model()
        value = disclosure(strength_model)

        with self.assertRaisesRegex(ValueError, "league snapshot"):
            value.validate_bundle(
                snapshot_id="snapshot-2", strength_model=strength_model
            )
        with self.assertRaisesRegex(ValueError, "strength model"):
            value.validate_bundle(
                snapshot_id="snapshot-1",
                strength_model=model(residual=2),
            )
        with self.assertRaisesRegex(ValueError, "StrengthModel"):
            value.validate_bundle(snapshot_id="snapshot-1", strength_model=object())

    def test_is_immutable(self):
        value = disclosure()

        with self.assertRaises(FrozenInstanceError):
            value.policy_id = "changed"


if __name__ == "__main__":
    unittest.main()
