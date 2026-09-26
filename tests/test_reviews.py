import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}
ACTIVATE_FLOW = [
    ('assess', 'intake_officer', {'assessment_note': '收入波动'}),
    ('approve', 'underwriter', {'exception_approved': False}),
    ('activate', 'servicer', {'borrower_ack': True}),
]

SERVICER = Actor("svc-1", "servicer")
REVIEWER = Actor("rev-1", "reviewer")


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def _activate(self, reference="MORT-28001"):
        record = self.service.create(Actor("creator", "intake_officer"), reference, CREATE_DATA)
        for action, role, data in ACTIVATE_FLOW:
            record = self.service.act(Actor("operator", role), record["id"], record["version"], action, data)
        self.assertEqual(record["state"], "active")
        return record

    def test_first_review_below_exit_threshold_recommends_exit(self):
        record = self._activate()
        review = self.service.submit_review(SERVICER, record["id"], {
            'avg_monthly_income': 20000, 'monthly_expenses': 8000, 'new_debt_payment': 0})
        self.assertEqual(review["seq"], 1)
        self.assertAlmostEqual(review["affordability_ratio"], 3400 / 20000, places=4)
        self.assertEqual(review["recommendation"], "exit")
        self.assertEqual(review["status"], "pending")
        self.assertIsNone(review["decision"])

    def test_two_consecutive_high_ratios_recommend_extension(self):
        record = self._activate()
        first = self.service.submit_review(SERVICER, record["id"], {
            'avg_monthly_income': 8000, 'monthly_expenses': 4000, 'new_debt_payment': 0})
        self.assertEqual(first["recommendation"], "none")
        second = self.service.submit_review(SERVICER, record["id"], {
            'avg_monthly_income': 7000, 'monthly_expenses': 4000, 'new_debt_payment': 500})
        self.assertEqual(second["recommendation"], "extend")
        self.assertEqual(second["status"], "pending")

    def test_threshold_boundaries_do_not_trigger(self):
        record = self._activate()
        at_extend = self.service.submit_review(SERVICER, record["id"], {
            'avg_monthly_income': 8500, 'monthly_expenses': 3000, 'new_debt_payment': 0})
        self.assertAlmostEqual(at_extend["affordability_ratio"], 0.4, places=4)
        self.assertEqual(at_extend["recommendation"], "none")
        at_exit = self.service.submit_review(SERVICER, record["id"], {
            'avg_monthly_income': 13600, 'monthly_expenses': 3000, 'new_debt_payment': 0})
        self.assertAlmostEqual(at_exit["affordability_ratio"], 0.25, places=4)
        self.assertEqual(at_exit["recommendation"], "none")

    def test_high_then_normal_does_not_recommend_extension(self):
        record = self._activate()
        self.service.submit_review(SERVICER, record["id"], {
            'avg_monthly_income': 8000, 'monthly_expenses': 4000, 'new_debt_payment': 0})
        recovered = self.service.submit_review(SERVICER, record["id"], {
            'avg_monthly_income': 12000, 'monthly_expenses': 4000, 'new_debt_payment': 0})
        self.assertEqual(recovered["recommendation"], "none")

    def test_reviewer_confirms_adopt_and_keep(self):
        record = self._activate()
        review = self.service.submit_review(SERVICER, record["id"], {
            'avg_monthly_income': 20000, 'monthly_expenses': 8000, 'new_debt_payment': 0})
        confirmed = self.service.decide_review(REVIEWER, record["id"], review["id"], {
            'decision': 'adopt', 'review_note': '启动退出'})
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["decision"], "adopt")
        self.assertEqual(confirmed["decided_by"], "rev-1")
        with self.assertRaises(Conflict):
            self.service.decide_review(REVIEWER, record["id"], review["id"], {'decision': 'keep'})

        second = self.service.submit_review(SERVICER, record["id"], {
            'avg_monthly_income': 12000, 'monthly_expenses': 4000, 'new_debt_payment': 0})
        kept = self.service.decide_review(REVIEWER, record["id"], second["id"], {'decision': 'keep'})
        self.assertEqual(kept["decision"], "keep")

    def test_plan_continues_before_reviewer_confirms(self):
        record = self._activate()
        self.service.submit_review(SERVICER, record["id"], {
            'avg_monthly_income': 20000, 'monthly_expenses': 8000, 'new_debt_payment': 0})
        unchanged = self.service.get_record(REVIEWER, record["id"])
        self.assertEqual(unchanged["state"], "active")
        self.assertEqual(unchanged["version"], record["version"])
        reviews = self.service.list_reviews(REVIEWER, record["id"])
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["status"], "pending")

    def test_review_permissions(self):
        record = self._activate()
        with self.assertRaises(PermissionDenied):
            self.service.submit_review(Actor("rev", "reviewer"), record["id"], {
                'avg_monthly_income': 20000, 'monthly_expenses': 8000, 'new_debt_payment': 0})
        review = self.service.submit_review(SERVICER, record["id"], {
            'avg_monthly_income': 20000, 'monthly_expenses': 8000, 'new_debt_payment': 0})
        with self.assertRaises(PermissionDenied):
            self.service.decide_review(SERVICER, record["id"], review["id"], {'decision': 'adopt'})

    def test_review_only_for_active_plan_and_validated(self):
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-28002", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.submit_review(SERVICER, record["id"], {
                'avg_monthly_income': 20000, 'monthly_expenses': 8000, 'new_debt_payment': 0})
        record = self._activate("MORT-28003")
        with self.assertRaises(ValidationError):
            self.service.submit_review(SERVICER, record["id"], {
                'avg_monthly_income': 0, 'monthly_expenses': 8000, 'new_debt_payment': 0})
        review = self.service.submit_review(SERVICER, record["id"], {
            'avg_monthly_income': 20000, 'monthly_expenses': 8000, 'new_debt_payment': 0})
        with self.assertRaises(ValidationError):
            self.service.decide_review(REVIEWER, record["id"], review["id"], {'decision': 'maybe'})

    def test_reviews_persist_and_follow_timeline(self):
        record = self._activate("MORT-28004")
        review = self.service.submit_review(SERVICER, record["id"], {
            'avg_monthly_income': 20000, 'monthly_expenses': 8000, 'new_debt_payment': 0, 'note': '借款人电话反馈'})
        self.service.decide_review(REVIEWER, record["id"], review["id"], {'decision': 'adopt'})
        rebuilt = build_service(str(Path(self.temp.name) / "test.db"))
        reviews = rebuilt.list_reviews(SERVICER, record["id"])
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["recommendation"], "exit")
        self.assertEqual(reviews[0]["decision"], "adopt")
        self.assertEqual(reviews[0]["note"], "借款人电话反馈")
        actions = [event["action"] for event in rebuilt.timeline(SERVICER, record["id"])]
        self.assertIn("review", actions)
        self.assertIn("review_decision", actions)
        self.assertEqual(actions.index("review") < actions.index("review_decision"), True)
