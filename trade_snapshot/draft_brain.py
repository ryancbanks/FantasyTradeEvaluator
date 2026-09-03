"""Deterministic, portable neural rankers for anonymous draft candidates."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from math import fsum, isfinite
from numbers import Real
from operator import mul
import re

from ._scenario_random import SAFE_INTEGER, canonical_json, content_id


_FEATURE_SCHEMA_VERSION = 1
_BASELINE_SCHEMA_VERSION = 1
_BRAIN_SCHEMA_VERSION = 1
_NETWORK_ALGORITHM = "fixed-point-residual-16x8x1-leaky-relu-v1"
_SCALE = 1_000_000
_HIDDEN_1 = 16
_HIDDEN_2 = 8
_MAX_FEATURES = 256
_MAX_VECTOR_SIZE = 512
_MAX_BATCH_SIZE = 4096
_MAX_ABS_NUMBER = 1.0e15
_MAX_WEIGHT = 10 * _SCALE
_ACTIVATION_BOUND = 8 * _SCALE
_NETWORK_INPUT_BOUND = 32.0
_FEATURE_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_FINGERPRINT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
_CONTENT_IDS = {
    "draft_features": re.compile(r"^draft_features_[0-9a-f]{64}$"),
    "draft_baseline": re.compile(r"^draft_baseline_[0-9a-f]{64}$"),
    "draft_brain": re.compile(r"^draft_brain_[0-9a-f]{64}$"),
}
_IDENTITY_NAMES = frozenset(
    {"id", "name", "player", "player_id", "player_name", "canonical_player_id",
     "provider_player_id", "nfl_team", "nfl_team_id", "team_id", "club"}
)


@dataclass(frozen=True, slots=True)
class FeatureSchema:
    """Ordered numeric features and training-only normalization parameters.

    A feature listed in ``missing_indicators`` may be absent or ``None``. Its
    normalized value is then zero and a trailing 1.0 indicator is emitted.
    Unknown fields are rejected so names and identifiers cannot accidentally
    become model inputs.
    """

    names: tuple[str, ...]
    means: tuple[float, ...]
    scales: tuple[float, ...]
    missing_indicators: tuple[str, ...] = ()
    feature_schema_id: str = field(init=False)
    _missing_indicator_set: frozenset[str] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        names = _feature_names(self.names)
        means = _number_tuple("means", self.means, len(names))
        scales = _number_tuple("scales", self.scales, len(names), positive=True)
        indicators = _text_tuple("missing_indicators", self.missing_indicators)
        if len(set(indicators)) != len(indicators) or not set(indicators).issubset(names):
            raise ValueError("missing_indicators must be unique feature names")
        if indicators != tuple(name for name in names if name in set(indicators)):
            raise ValueError("missing_indicators must follow feature order")
        if len(names) + len(indicators) > _MAX_VECTOR_SIZE:
            raise ValueError("encoded feature vector is too large")
        object.__setattr__(self, "names", names)
        object.__setattr__(self, "means", means)
        object.__setattr__(self, "scales", scales)
        object.__setattr__(self, "missing_indicators", indicators)
        object.__setattr__(self, "_missing_indicator_set", frozenset(indicators))
        object.__setattr__(self, "feature_schema_id", content_id("draft_features", self._content()))

    @property
    def vector_size(self) -> int:
        return len(self.names) + len(self.missing_indicators)

    def encode(self, values: Mapping[str, Real | None]) -> tuple[float, ...]:
        if not isinstance(values, Mapping):
            raise ValueError("candidate features must be a mapping")
        unknown = set(values).difference(self.names)
        if unknown:
            raise ValueError(f"candidate features contain unknown field {min(map(str, unknown))!r}")
        encoded: list[float] = []
        missing: list[float] = []
        for name, mean, scale in zip(self.names, self.means, self.scales):
            raw = values.get(name)
            is_missing = name not in values or raw is None
            if is_missing and name not in self._missing_indicator_set:
                raise ValueError(f"candidate feature {name!r} is required")
            if is_missing:
                encoded.append(0.0)
            else:
                normalized = (_number(f"candidate feature {name!r}", raw) - mean) / scale
                if not isfinite(normalized) or abs(normalized) > _MAX_ABS_NUMBER:
                    raise ValueError(f"candidate feature {name!r} normalizes outside safe bounds")
                encoded.append(0.0 if normalized == 0 else normalized)
            if name in self._missing_indicator_set:
                missing.append(1.0 if is_missing else 0.0)
        return tuple((*encoded, *missing))

    def _content(self) -> dict[str, object]:
        return {"missing_indicators": list(self.missing_indicators), "names": list(self.names),
                "means": list(self.means), "scales": list(self.scales)}

    def to_record(self) -> dict[str, object]:
        return {"kind": "fantasy_draft_feature_schema", "schema_version": _FEATURE_SCHEMA_VERSION,
                **self._content(), "feature_schema_id": self.feature_schema_id}

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> FeatureSchema:
        keys = {"kind", "schema_version", "names", "means", "scales",
                "missing_indicators", "feature_schema_id"}
        _record(record, keys, "feature schema")
        if (record["kind"] != "fantasy_draft_feature_schema" or
                type(record["schema_version"]) is not int or record["schema_version"] != 1):
            raise ValueError("feature schema kind or version is invalid")
        names = _array(record, "names", maximum=_MAX_FEATURES)
        schema = cls(
            names,
            _array(record, "means", length=len(names)),
            _array(record, "scales", length=len(names)),
            _array(record, "missing_indicators", maximum=len(names)),
        )
        _verify_id("draft_features", record["feature_schema_id"], schema.feature_schema_id)
        return schema


@dataclass(frozen=True, slots=True)
class RegressionBaseline:
    feature_schema_id: str
    coefficients: tuple[float, ...]
    intercept: float = 0.0
    baseline_id: str = field(init=False)

    def __post_init__(self) -> None:
        _require_id("draft_features", self.feature_schema_id)
        coefficients = _number_tuple("coefficients", self.coefficients)
        if not coefficients or len(coefficients) > _MAX_VECTOR_SIZE:
            raise ValueError("coefficients must have a supported non-zero length")
        intercept = _number("intercept", self.intercept)
        object.__setattr__(self, "coefficients", coefficients)
        object.__setattr__(self, "intercept", intercept)
        object.__setattr__(self, "baseline_id", content_id("draft_baseline", self._content()))

    def score(self, encoded: Sequence[Real]) -> float:
        vector = _encoded_vector(encoded, len(self.coefficients))
        return self._score_validated(vector)

    def _score_validated(self, vector: tuple[float, ...]) -> float:
        try:
            result = fsum((self.intercept, *(value * weight for value, weight in zip(vector, self.coefficients))))
        except OverflowError:
            raise ValueError("baseline score is not finite") from None
        if not isfinite(result):
            raise ValueError("baseline score is not finite")
        return result

    def _content(self) -> dict[str, object]:
        return {"feature_schema_id": self.feature_schema_id,
                "coefficients": list(self.coefficients), "intercept": self.intercept}

    def to_record(self) -> dict[str, object]:
        return {"kind": "fantasy_draft_regression_baseline",
                "schema_version": _BASELINE_SCHEMA_VERSION, **self._content(),
                "baseline_id": self.baseline_id}

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> RegressionBaseline:
        keys = {"kind", "schema_version", "feature_schema_id", "coefficients",
                "intercept", "baseline_id"}
        _record(record, keys, "regression baseline")
        if (record["kind"] != "fantasy_draft_regression_baseline" or
                type(record["schema_version"]) is not int or record["schema_version"] != 1):
            raise ValueError("regression baseline kind or version is invalid")
        baseline = cls(
            record["feature_schema_id"],
            _array(record, "coefficients", maximum=_MAX_VECTOR_SIZE),
            record["intercept"],
        )
        _verify_id("draft_baseline", record["baseline_id"], baseline.baseline_id)
        return baseline


@dataclass(frozen=True, slots=True)
class DraftBrain:
    schema: FeatureSchema
    baseline: RegressionBaseline
    league_config_fingerprint: str
    input_weights: tuple[int, ...]
    input_biases: tuple[int, ...]
    hidden_weights: tuple[int, ...]
    hidden_biases: tuple[int, ...]
    output_weights: tuple[int, ...]
    output_bias: int
    brain_id: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.schema, FeatureSchema) or not isinstance(self.baseline, RegressionBaseline):
            raise ValueError("schema and baseline must be their corresponding model types")
        if self.baseline.feature_schema_id != self.schema.feature_schema_id:
            raise ValueError("baseline does not match the feature schema")
        if len(self.baseline.coefficients) != self.schema.vector_size:
            raise ValueError("baseline coefficient count does not match encoded features")
        fingerprint = _fingerprint(self.league_config_fingerprint)
        layers = (
            _int_tuple("input_weights", self.input_weights, self.schema.vector_size * _HIDDEN_1),
            _int_tuple("input_biases", self.input_biases, _HIDDEN_1),
            _int_tuple("hidden_weights", self.hidden_weights, _HIDDEN_1 * _HIDDEN_2),
            _int_tuple("hidden_biases", self.hidden_biases, _HIDDEN_2),
            _int_tuple("output_weights", self.output_weights, _HIDDEN_2),
        )
        output_bias = _weight("output_bias", self.output_bias)
        object.__setattr__(self, "league_config_fingerprint", fingerprint)
        for name, value in zip(("input_weights", "input_biases", "hidden_weights",
                                "hidden_biases", "output_weights"), layers):
            object.__setattr__(self, name, value)
        object.__setattr__(self, "output_bias", output_bias)
        object.__setattr__(self, "brain_id", content_id("draft_brain", self._content()))

    @classmethod
    def zero_residual(cls, schema: FeatureSchema, baseline: RegressionBaseline,
                      league_config_fingerprint: str) -> DraftBrain:
        return cls(schema, baseline, league_config_fingerprint,
                   (0,) * (schema.vector_size * _HIDDEN_1), (0,) * _HIDDEN_1,
                   (0,) * (_HIDDEN_1 * _HIDDEN_2), (0,) * _HIDDEN_2,
                   (0,) * _HIDDEN_2, 0)

    @property
    def parameter_count(self) -> int:
        return len(self._parameters())

    def score(self, features: Mapping[str, Real | None]) -> float:
        return self.score_parts(features)[1]

    def score_parts(
        self, features: Mapping[str, Real | None]
    ) -> tuple[float, float]:
        """Return baseline and total utility for one anonymous feature map."""

        return self._score_validated_parts(self.schema.encode(features))

    def score_vector(self, encoded: Sequence[Real]) -> float:
        return self.score_vector_parts(encoded)[1]

    def score_vector_parts(self, encoded: Sequence[Real]) -> tuple[float, float]:
        """Return baseline and total utility with one validation/network pass."""

        return self._score_validated_parts(
            _encoded_vector(encoded, self.schema.vector_size)
        )

    def _score_validated_parts(
        self, vector: tuple[float, ...]
    ) -> tuple[float, float]:
        baseline = self.baseline._score_validated(vector)
        if self.output_bias == 0 and not any(self.output_weights):
            return baseline, baseline
        fixed = tuple(round(max(-_NETWORK_INPUT_BOUND, min(_NETWORK_INPUT_BOUND, value)) * _SCALE)
                      for value in vector)
        first = _dense(fixed, self.input_weights, self.input_biases, _HIDDEN_1, True)
        second = _dense(first, self.hidden_weights, self.hidden_biases, _HIDDEN_2, True)
        residual = _dense(second, self.output_weights, (self.output_bias,), 1, False)[0] / _SCALE
        result = baseline + residual
        if not isfinite(result):
            raise ValueError("brain score is not finite")
        return baseline, result

    def score_candidates(self, candidates: Iterable[Mapping[str, Real | None]]) -> tuple[float, ...]:
        if isinstance(candidates, (str, bytes, Mapping)):
            raise ValueError("candidates must be an iterable of feature mappings")
        scores = []
        for index, candidate in enumerate(candidates):
            if index >= _MAX_BATCH_SIZE:
                raise ValueError(f"candidate batch cannot exceed {_MAX_BATCH_SIZE}")
            scores.append(self.score(candidate))
        return tuple(scores)

    def _parameters(self) -> tuple[int, ...]:
        return (*self.input_weights, *self.input_biases, *self.hidden_weights,
                *self.hidden_biases, *self.output_weights, self.output_bias)

    def _content(self) -> dict[str, object]:
        return {"feature_schema": self.schema.to_record(), "baseline": self.baseline.to_record(),
                "league_config_fingerprint": self.league_config_fingerprint,
                "architecture": [self.schema.vector_size, _HIDDEN_1, _HIDDEN_2, 1],
                "network_algorithm": _NETWORK_ALGORITHM, "quantization_scale": _SCALE,
                "input_weights": list(self.input_weights), "input_biases": list(self.input_biases),
                "hidden_weights": list(self.hidden_weights), "hidden_biases": list(self.hidden_biases),
                "output_weights": list(self.output_weights), "output_bias": self.output_bias}

    def to_record(self) -> dict[str, object]:
        return {"kind": "fantasy_draft_brain", "schema_version": _BRAIN_SCHEMA_VERSION,
                **self._content(), "brain_id": self.brain_id}

    @classmethod
    def from_record(cls, record: Mapping[str, object]) -> DraftBrain:
        content = {"feature_schema", "baseline", "league_config_fingerprint", "architecture",
                   "network_algorithm", "quantization_scale", "input_weights", "input_biases",
                   "hidden_weights", "hidden_biases", "output_weights", "output_bias"}
        _record(record, content | {"kind", "schema_version", "brain_id"}, "draft brain")
        if (record["kind"] != "fantasy_draft_brain" or
                type(record["schema_version"]) is not int or record["schema_version"] != 1):
            raise ValueError("draft brain kind or version is invalid")
        if (record["network_algorithm"] != _NETWORK_ALGORITHM or
                type(record["quantization_scale"]) is not int or
                record["quantization_scale"] != _SCALE):
            raise ValueError("draft brain network algorithm is unsupported")
        schema_record = record["feature_schema"]
        baseline_record = record["baseline"]
        if not isinstance(schema_record, Mapping) or not isinstance(baseline_record, Mapping):
            raise ValueError("draft brain schema and baseline must be JSON objects")
        schema = FeatureSchema.from_record(schema_record)
        architecture = record["architecture"]
        if (not isinstance(architecture, list) or
                any(type(value) is not int for value in architecture) or
                architecture != [schema.vector_size, _HIDDEN_1, _HIDDEN_2, 1]):
            raise ValueError("draft brain architecture is invalid")
        brain = cls(
            schema,
            RegressionBaseline.from_record(baseline_record),
            record["league_config_fingerprint"],
            _array(record, "input_weights", length=schema.vector_size * _HIDDEN_1),
            _array(record, "input_biases", length=_HIDDEN_1),
            _array(record, "hidden_weights", length=_HIDDEN_1 * _HIDDEN_2),
            _array(record, "hidden_biases", length=_HIDDEN_2),
            _array(record, "output_weights", length=_HIDDEN_2),
            record["output_bias"],
        )
        _verify_id("draft_brain", record["brain_id"], brain.brain_id)
        return brain


def initialize_genome(schema: FeatureSchema, baseline: RegressionBaseline,
                      league_config_fingerprint: str, *, seed: int, genome_index: int,
                      magnitude: int = 150_000) -> DraftBrain:
    """Create one stable residual genome without global pseudo-random state."""
    _seed_parts(seed, genome_index)
    magnitude = _magnitude(magnitude)
    empty = DraftBrain.zero_residual(schema, baseline, league_config_fingerprint)
    stream = _HashStream({"domain": "draft-brain-init-v1", "seed": seed,
                          "genome_index": genome_index, "schema_id": schema.feature_schema_id,
                          "config": empty.league_config_fingerprint})
    return _brain_from_parameters(empty, tuple(_signed(stream, magnitude)
                                                for _ in range(empty.parameter_count)))


def crossover_and_mutate(left: DraftBrain, right: DraftBrain, *, seed: int,
                         generation: int, offspring_index: int,
                         mutation_rate: float = 0.05,
                         mutation_magnitude: int = 25_000) -> DraftBrain:
    """Recombine compatible parents using a content-keyed deterministic stream."""
    if not isinstance(left, DraftBrain) or not isinstance(right, DraftBrain):
        raise ValueError("parents must be DraftBrain values")
    if (left.schema != right.schema or left.baseline != right.baseline or
            left.league_config_fingerprint != right.league_config_fingerprint):
        raise ValueError("parents must share schema, baseline, and league configuration")
    _seed_parts(seed, generation, offspring_index)
    rate = _number("mutation_rate", mutation_rate)
    if not 0 <= rate <= 1:
        raise ValueError("mutation_rate must be between zero and one")
    magnitude = _magnitude(mutation_magnitude)
    stream = _HashStream({"domain": "draft-brain-reproduction-v1", "seed": seed,
                          "generation": generation, "offspring_index": offspring_index,
                          "left": left.brain_id, "right": right.brain_id})
    threshold = int(rate * (1 << 64))
    child = []
    for first, second in zip(left._parameters(), right._parameters()):
        value = first if stream.next_u64() & 1 == 0 else second
        if stream.next_u64() < threshold:
            value = max(-_MAX_WEIGHT, min(_MAX_WEIGHT, value + _signed(stream, magnitude)))
        child.append(value)
    return _brain_from_parameters(left, tuple(child))


class _HashStream:
    def __init__(self, identity: Mapping[str, object]) -> None:
        self._key = canonical_json(identity).encode("utf-8")
        self._counter = 0
        self._buffer = b""

    def next_u64(self) -> int:
        if len(self._buffer) < 8:
            self._buffer += sha256(self._key + self._counter.to_bytes(8, "big")).digest()
            self._counter += 1
        result = int.from_bytes(self._buffer[:8], "big")
        self._buffer = self._buffer[8:]
        return result


def _brain_from_parameters(template: DraftBrain, values: tuple[int, ...]) -> DraftBrain:
    a = len(template.input_weights); b = a + len(template.input_biases)
    c = b + len(template.hidden_weights); d = c + len(template.hidden_biases)
    e = d + len(template.output_weights)
    if len(values) != e + 1:
        raise ValueError("parameter vector has the wrong length")
    return DraftBrain(template.schema, template.baseline, template.league_config_fingerprint,
                      values[:a], values[a:b], values[b:c], values[c:d], values[d:e], values[e])


def _dense(inputs, weights, biases, output_count: int, activate: bool) -> tuple[int, ...]:
    width = len(inputs); result = []
    for output in range(output_count):
        start = output * width
        if width >= 8:
            # ``map`` performs the wide inner product loop in C.  Small layers
            # stay indexed because allocating a slice costs more than it saves.
            total = biases[output] * _SCALE + sum(
                map(mul, weights[start:start + width], inputs)
            )
        else:
            total = biases[output] * _SCALE
            for input_index, value in enumerate(inputs):
                total += weights[start + input_index] * value
        value = _rounded_divide(total, _SCALE)
        if activate and value < 0:
            value = -((-value) // 100)
        result.append(max(-_ACTIVATION_BOUND, min(_ACTIVATION_BOUND, value)))
    return tuple(result)


def _rounded_divide(value: int, divisor: int) -> int:
    sign = -1 if value < 0 else 1
    return sign * ((abs(value) + divisor // 2) // divisor)


def _feature_names(values) -> tuple[str, ...]:
    names = _text_tuple("names", values)
    if not names or len(names) > _MAX_FEATURES or len(set(names)) != len(names):
        raise ValueError("feature names must be unique and within the supported size")
    for name in names:
        if not _FEATURE_NAME.fullmatch(name):
            raise ValueError("feature names must be lowercase portable identifiers")
        tokens = set(re.split(r"[_.-]", name))
        if (name in _IDENTITY_NAMES or
                tokens.intersection({"id", "name", "identity", "identifier"})):
            raise ValueError(f"identity feature {name!r} is forbidden")
    return names


def _number(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name} must be a finite number")
    try: result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{name} must be a finite number") from None
    if not isfinite(result) or abs(result) > _MAX_ABS_NUMBER:
        raise ValueError(f"{name} must be a finite number within safe bounds")
    return 0.0 if result == 0 else result


def _number_tuple(name, values, length=None, *, positive=False) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a numeric sequence")
    try: result = tuple(_number(f"{name} value", value) for value in values)
    except TypeError: raise ValueError(f"{name} must be a numeric sequence") from None
    if length is not None and len(result) != length:
        raise ValueError(f"{name} must contain exactly {length} values")
    if positive and any(value <= 0 for value in result):
        raise ValueError(f"{name} values must be positive")
    return result


def _text_tuple(name, values) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be a sequence")
    try: result = tuple(values)
    except TypeError: raise ValueError(f"{name} must be a sequence") from None
    if any(not isinstance(value, str) or not value for value in result):
        raise ValueError(f"{name} must contain non-empty strings")
    return result


def _encoded_vector(values, length) -> tuple[float, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("encoded features must be a numeric sequence")
    return _number_tuple("encoded features", values, length)


def _int_tuple(name, values, length) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{name} must be an integer sequence")
    try: result = tuple(_weight(f"{name} value", value) for value in values)
    except TypeError: raise ValueError(f"{name} must be an integer sequence") from None
    if len(result) != length:
        raise ValueError(f"{name} must contain exactly {length} values")
    return result


def _weight(name, value) -> int:
    if type(value) is not int or abs(value) > _MAX_WEIGHT:
        raise ValueError(f"{name} must be a bounded integer weight")
    return value


def _fingerprint(value) -> str:
    if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
        raise ValueError("league_config_fingerprint must be a portable fingerprint")
    return value


def _record(record, keys, name) -> None:
    if not isinstance(record, Mapping) or set(record) != keys:
        raise ValueError(f"{name} record fields are invalid")


def _array(record, name, *, length=None, maximum=None) -> tuple:
    value = record[name]
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array")
    if length is not None and len(value) != length:
        raise ValueError(f"{name} must contain exactly {length} values")
    if maximum is not None and len(value) > maximum:
        raise ValueError(f"{name} exceeds the supported size")
    return tuple(value)


def _require_id(prefix, value) -> None:
    if not isinstance(value, str) or not _CONTENT_IDS[prefix].fullmatch(value):
        raise ValueError(f"{prefix} identifier is invalid")


def _verify_id(prefix, supplied, actual) -> None:
    _require_id(prefix, supplied)
    if supplied != actual:
        raise ValueError(f"{prefix} content does not match its identifier")


def _seed_parts(*parts) -> None:
    if any(type(part) is not int or part < 0 or part > SAFE_INTEGER for part in parts):
        raise ValueError("seed and index values must be non-negative JSON-safe integers")


def _magnitude(value) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_WEIGHT:
        raise ValueError("mutation magnitude must be a supported non-negative integer")
    return value


def _signed(stream: _HashStream, magnitude: int) -> int:
    return int(stream.next_u64() % (2 * magnitude + 1)) - magnitude


__all__ = ("FeatureSchema", "RegressionBaseline", "DraftBrain",
           "initialize_genome", "crossover_and_mutate")
