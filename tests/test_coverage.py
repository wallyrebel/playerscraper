import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import top_performers as tp


class CoverageTests(unittest.TestCase):
    def result(self, ok=True, **metrics):
        return tp.SportDateResult(tp.SPORT_CONFIGS[0], "10/26/2026", scoreboard_ok=ok,
                                  error=None if ok else "HTTP 406", metrics={**tp.new_metrics(), **metrics})

    def test_complete_empty_partial_failed_are_distinct(self):
        window = [date(2026, 10, 25), date(2026, 10, 26)]
        for results, state in [([self.result()], "complete"),
                               ([self.result(), self.result(False)], "partial"),
                               ([self.result(False)] * 12, "failed")]:
            self.assertEqual(tp.coverage_state(results), state)
            subject = tp.build_subject(False, [], window, results=results)
            body = tp.build_body([], window, results, 0)
            self.assertIn(state.upper(), body)
            if state == "complete":
                self.assertIn("No New Qualifying", subject)
            else:
                self.assertIn(state.upper(), subject)
                self.assertNotIn("No New Qualifying", subject)
                self.assertNotIn("No new players met", body)

    def test_game_failures_and_caps_are_partial(self):
        for metrics in [{"games_failed": 1}, {"games_capped": 1}]:
            self.assertEqual(tp.coverage_state([self.result(**metrics)]), "partial")

    def test_total_failure_returns_one_and_preserves_ledger_even_when_emailed(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = Path(directory) / "ledger.json"
            before = json.dumps({"entries": {"existing": {"sent": "unchanged"}}})
            ledger.write_text(before)
            import os
            old = os.getcwd()
            os.chdir(directory)
            try:
                with patch.object(tp, "run_scrape", return_value=([], [self.result(False)] * 12)), \
                     patch.object(tp, "send_email") as send:
                    code = tp.main(["--ledger", str(ledger), "--only-when-new"])
                self.assertEqual(code, 1)
                self.assertEqual(ledger.read_text(), before)
                self.assertIn("FAILED", send.call_args.args[0])
                self.assertEqual(json.loads(Path("run-report.json").read_text())["coverage"], "failed")
            finally:
                os.chdir(old)

    def test_access_denial_not_retried_and_blank_200_is_failure(self):
        for status in [403, 406]:
            page = Mock()
            page.goto.return_value.status = status
            with self.assertRaises(tp.SourceAccessError):
                tp.fetch_scoreboard(page, "https://source.example")
            self.assertEqual(page.goto.call_count, 1)
            page.wait_for_timeout.assert_not_called()
        page = Mock()
        page.goto.return_value.status = 200
        page.content.return_value = "<html>Loading</html>"
        with self.assertRaisesRegex(RuntimeError, "no recognizable"):
            tp.fetch_scoreboard(page, "https://source.example", attempts=1)
        page.content.return_value = "<html>No games scheduled</html>"
        self.assertIn("No games", tp.fetch_scoreboard(page, "https://source.example"))

    def test_game_http_failure_is_not_awaiting_stats(self):
        page = Mock()
        page.goto.return_value.status = 406
        with self.assertRaises(tp.SourceAccessError):
            tp.process_game(page, tp.SPORT_CONFIGS[0], "https://source.example/game", tp.new_metrics())
        self.assertEqual(page.goto.call_count, 1)

    def test_other_date_scoreboard_is_not_complete_empty(self):
        with patch.object(tp, "fetch_scoreboard", return_value="games"), \
             patch.object(tp, "discover_games", return_value=([], {"links_seen": 4, "wrong_date": 4, "non_ms": 0})):
            result = tp.process_sport_date(Mock(), tp.SPORT_CONFIGS[0], date(2026, 10, 26), 10)
        self.assertEqual(result.coverage, "failed")
        self.assertIn("only other dates", result.error)

    def test_caught_game_exception_records_partial(self):
        with patch.object(tp, "fetch_scoreboard", return_value="games"), \
             patch.object(tp, "discover_games", return_value=(["game"], {"links_seen": 1, "wrong_date": 0, "non_ms": 0})), \
             patch.object(tp, "process_game", side_effect=RuntimeError("HTTP 406")), \
             patch.object(tp, "random_delay"):
            result = tp.process_sport_date(Mock(), tp.SPORT_CONFIGS[0], date(2026, 10, 26), 10)
        self.assertEqual(result.coverage, "partial")
        self.assertEqual(result.metrics["games_failed"], 1)


if __name__ == "__main__":
    unittest.main()
