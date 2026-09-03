from importlib.resources import files
import unittest


class DraftUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assets = files("trade_snapshot.web_assets")
        cls.page = assets.joinpath("index.html").read_text(encoding="utf-8")
        cls.tabs = assets.joinpath("app_tabs.js").read_text(encoding="utf-8")
        cls.draft = assets.joinpath("draft_lab.js").read_text(encoding="utf-8")
        cls.styles = assets.joinpath("draft_lab.css").read_text(encoding="utf-8")

    def test_page_has_one_accessible_two_surface_tabset(self):
        self.assertIn('role="tablist"', self.page)
        self.assertIn('id="tradeLabTab"', self.page)
        self.assertIn('id="draftLabTab"', self.page)
        self.assertIn('id="tradeLabPanel"', self.page)
        self.assertIn('id="draftLabPanel"', self.page)
        self.assertIn('aria-controls="draftLabPanel"', self.page)
        self.assertIn('aria-labelledby="draftLabTab"', self.page)
        self.assertIn('role="tabpanel"', self.page)
        self.assertIn('class="draft-main" role="main"', self.page)
        self.assertIn('<div id="tradeLabPanel"', self.page)
        self.assertIn("<main>", self.page)
        self.assertIn("ArrowLeft", self.tabs)
        self.assertIn("ArrowRight", self.tabs)

    def test_draft_lab_exposes_the_complete_workflow(self):
        required_ids = (
            "draftCorpusFile", "draftModelFile", "draftBoardFile",
            "draftPreset", "draftPresetNotice", "draftTeamCount", "draftStarterSlots",
            "draftBenchSlots", "draftScoringWeights", "draftRegularWeeks",
            "draftPlayoffWeeks", "draftPlayoffTeams", "draftPopulation",
            "draftGenerations", "draftAppearances", "draftCandidateWindow",
            "draftSeed", "draftEstimateButton", "draftStartButton",
            "draftCancelButton", "draftProgress", "draftDownloadModel",
            "draftCheckpoint", "draftResumeButton", "draftPromoteButton",
            "draftHistoryBody", "draftShowcaseTeam", "draftRosterBody",
            "draftWeekBody", "draftStandingsBody", "draftBracketBody",
            "draftBenchmarkButton", "draftBenchmarkScope", "draftBenchmarkMetrics",
            "assistantModel", "assistantBoard", "assistantUserSlot",
            "assistantStrategy", "assistantPlayerSearch", "assistantPlayer",
            "assistantRecordPick", "assistantUndo", "assistantRecommendations",
            "assistantSession", "assistantOpen",
            "assistantEspnLeague", "assistantEspnSeason", "assistantEspnSync",
            "assistantEspnAuto",
        )
        for element_id in required_ids:
            with self.subTest(element_id=element_id):
                self.assertIn(f'id="{element_id}"', self.page)

        for label in (
            "Copy supported league structure", "Did it get worse?", "100 paired scenarios",
            "Last batch", "Autosaved", "Manual draft assistant",
        ):
            self.assertIn(label, self.page)

    def test_training_years_and_strategies_are_explicit_and_safe(self):
        for year in (*range(2015, 2020), *range(2021, 2026)):
            self.assertIn(f'value="{year}" checked', self.page)
        self.assertNotIn('value="2020"', self.page)
        for strategy in (
            "none", "streaming_qb", "streaming_te", "streaming_dst",
            "late_round_qb",
        ):
            self.assertIn(f'data-strategy="{strategy}"', self.page)
        self.assertIn('$("draftStrategyTotal")', self.draft)
        self.assertIn("syncTrainingYears", self.draft)
        self.assertIn("input.disabled = Boolean(corpus) && !available", self.draft)

    def test_assets_load_after_app_and_never_inject_html(self):
        self.assertLess(self.page.index('src="/app.js"'), self.page.index('src="/app_tabs.js"'))
        self.assertLess(self.page.index('src="/app_tabs.js"'), self.page.index('src="/draft_lab.js"'))
        self.assertIn('href="/draft_lab.css"', self.page)
        self.assertNotIn("innerHTML", self.tabs)
        self.assertNotIn("innerHTML", self.draft)
        self.assertIn("textContent", self.draft)

    def test_estimate_uses_the_season_fair_contract(self):
        self.assertIn("value.training_season_count", self.draft)
        self.assertIn("value.brain_appearances", self.draft)
        self.assertNotIn("value.drafts", self.draft)
        self.assertIn("manual draft room", self.page)
        self.assertIn("result.scope_notice", self.draft)
        self.assertIn("Season-clustered 95% interval", self.draft)

    def test_saved_work_can_be_resumed_without_overclaiming_presets(self):
        self.assertIn('"/api/draft/trainings/resume"', self.draft)
        self.assertIn('/api/draft/checkpoints/${checkpointId}/promote', self.draft)
        self.assertIn("checkpoint_job_id", self.draft)
        self.assertIn('generations: integer("draftGenerations")', self.draft)
        self.assertIn("checkpoint.generation_completed + 1", self.draft)
        self.assertIn("promotionBusy", self.draft)
        self.assertIn("/api/draft/assistants/${sessionId}", self.draft)
        self.assertIn("preset.compatibility_notice", self.draft)
        self.assertIn('option.disabled = !row.config || row.status === "unsupported"', self.draft)
        self.assertIn("No saved checkpoints yet", self.page)
        self.assertIn("No saved draft rooms yet", self.page)
        self.assertIn("jobLaunching", self.draft)
        self.assertIn("assistantBusy", self.draft)
        self.assertIn("selectCheckpointId: current.job_id", self.draft)
        self.assertIn("must form a complete bracket", self.draft)
        self.assertIn("Stop and keep last autosave", self.page)
        self.assertIn("Stop and keep last autosave", self.draft)

    def test_active_draft_job_reattaches_to_the_existing_monitor(self):
        self.assertIn('api("/api/activity")', self.draft)
        self.assertIn("restoreDraftJob(activity.draft)", self.draft)
        self.assertIn('addEventListener("serveractivitychange"', self.draft)
        self.assertIn("void monitorJob(job)", self.draft)
        self.assertIn("Boolean(pendingRecoveredJob)", self.draft)
        claim = self.draft.index("pendingRecoveredJob = activity.draft || null")
        self.assertLess(
            claim,
            self.draft.index("await refreshCatalog()", claim),
        )
        self.assertIn("Draft Lab may still be running locally", self.draft)
        self.assertIn("activity-ack", self.draft)
        self.assertIn("if (!job || activeJob || jobLaunching) return", self.draft)
        self.assertNotIn("setInterval(monitorJob", self.draft)
        self.assertNotIn("FteActiveJobs", self.draft)

    def test_public_espn_sync_is_optional_credential_free_and_guarded(self):
        self.assertIn("Public ESPN snake drafts only", self.page)
        self.assertIn("No cookies or credentials", self.page)
        self.assertIn("/espn-sync", self.draft)
        self.assertIn("assistantSyncBusy", self.draft)
        self.assertIn("15000", self.draft)
        self.assertIn("document.hidden", self.draft)
        self.assertIn("status.draft_binding", self.draft)
        self.assertIn("binding.league_id", self.draft)
        self.assertIn("binding.season", self.draft)

    def test_draft_styles_are_dark_responsive_and_motion_safe(self):
        self.assertIn("color-scheme: dark", self.styles)
        self.assertIn("@media (max-width:", self.styles)
        self.assertIn("prefers-reduced-motion: reduce", self.styles)
        self.assertIn(".app-surface:focus-visible", self.styles)
        self.assertNotIn(".app-surface:focus { outline: none", self.styles)
        self.assertNotIn("color-scheme: light", self.styles)


if __name__ == "__main__":
    unittest.main()
