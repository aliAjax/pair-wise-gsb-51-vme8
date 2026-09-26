import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'monthly_income': 18000.0, 'monthly_expenses': 9000.0, 'monthly_payment': 7000.0, 'arrears': 12000.0, 'hardship_factor': 0.5, 'program_type': 'reduction', 'requested_months': 9}
# reduction方案：批准月供 = 7000 - (18000-9000)*0.4 = 3400


def activate(service, reference="MORT-30001"):
    record = service.create(Actor("creator", "intake_officer"), reference, CREATE_DATA)
    record = service.act(Actor("op", "intake_officer"), record["id"], record["version"], "assess", {"assessment_note": "收入波动"})
    record = service.act(Actor("op", "underwriter"), record["id"], record["version"], "approve", {"exception_approved": False})
    record = service.act(Actor("op", "servicer"), record["id"], record["version"], "activate", {"borrower_ack": True})
    return record


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def review(self, record_id, income, expenses=5000.0, debt=0.0):
        return self.service.submit_review(Actor("staff", "servicer"), record_id, {"monthly_income": income, "household_expenses": expenses, "new_debt_payment": debt, "note": ""})

    def test_extend_after_two_consecutive_high_and_confirm(self):
        record = activate(self.service)
        version = record["version"]
        r1 = self.review(record["id"], 10000.0)
        self.assertEqual(r1["suggestion"], "maintain")
        self.assertEqual(r1["consecutive_high"], 0)
        self.assertAlmostEqual(r1["burden_ratio"], 0.34, places=3)
        r2 = self.review(record["id"], 7000.0)
        self.assertEqual(r2["suggestion"], "maintain")
        self.assertEqual(r2["consecutive_high"], 1)
        r3 = self.review(record["id"], 6000.0)
        self.assertEqual(r3["suggestion"], "extend")
        self.assertEqual(r3["consecutive_high"], 2)
        self.assertEqual(r3["status"], "pending")
        # 复核岗确认前原方案照常履约：状态与版本均不变
        unchanged = self.service.get_record(Actor("viewer", "reviewer"), record["id"])
        self.assertEqual(unchanged["state"], "active")
        self.assertEqual(unchanged["version"], version)
        decided = self.service.decide_review(Actor("boss", "reviewer"), record["id"], r3["id"], "confirmed", "同意展期")
        self.assertEqual(decided["status"], "confirmed")
        self.assertEqual(decided["decided_by"], "boss")
        self.assertEqual(decided["decision_note"], "同意展期")
        # 确认结果可见，方案状态仍由既有动作流控制
        after = self.service.get_record(Actor("viewer", "reviewer"), record["id"])
        self.assertEqual(after["state"], "active")
        self.assertEqual(len(after["reviews"]), 3)
        self.assertEqual(after["reviews"][2]["status"], "confirmed")
        timeline = self.service.timeline(Actor("viewer", "reviewer"), record["id"])
        actions = [event["action"] for event in timeline]
        self.assertEqual(actions.count("review_submitted"), 3)
        self.assertEqual(actions.count("review_decided"), 1)
        self.assertIn("建议展期", timeline[-1]["details"]["summary"])

    def test_exit_when_ratio_below_quarter(self):
        record = activate(self.service)
        review = self.review(record["id"], 20000.0)
        self.assertEqual(review["suggestion"], "exit")
        self.assertEqual(review["consecutive_high"], 0)

    def test_consecutive_high_resets_after_normal_review(self):
        record = activate(self.service)
        self.review(record["id"], 7000.0)
        reset = self.review(record["id"], 10000.0)
        self.assertEqual(reset["consecutive_high"], 0)
        again = self.review(record["id"], 7000.0)
        self.assertEqual(again["consecutive_high"], 1)
        self.assertEqual(again["suggestion"], "maintain")

    def test_zero_income_counts_as_high(self):
        record = activate(self.service)
        first = self.review(record["id"], 0.0)
        self.assertIsNone(first["burden_ratio"])
        self.assertEqual(first["consecutive_high"], 1)
        second = self.review(record["id"], 0.0)
        self.assertEqual(second["suggestion"], "extend")

    def test_review_requires_active_state(self):
        record = self.service.create(Actor("creator", "intake_officer"), "MORT-30002", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.review(record["id"], 10000.0)

    def test_review_permissions(self):
        record = activate(self.service)
        with self.assertRaises(PermissionDenied):
            self.service.submit_review(Actor("uw", "underwriter"), record["id"], {"monthly_income": 1.0, "household_expenses": 0.0, "new_debt_payment": 0.0})
        review = self.review(record["id"], 10000.0)
        with self.assertRaises(PermissionDenied):
            self.service.decide_review(Actor("staff", "servicer"), record["id"], review["id"], "confirmed")
        with self.assertRaises(PermissionDenied):
            self.service.decide_review(Actor("x", "outsider"), record["id"], review["id"], "confirmed")

    def test_review_validation(self):
        record = activate(self.service)
        with self.assertRaises(ValidationError):
            self.service.submit_review(Actor("staff", "servicer"), record["id"], {"household_expenses": 1.0})
        with self.assertRaises(ValidationError):
            self.service.submit_review(Actor("staff", "servicer"), record["id"], {"monthly_income": 1.0, "household_expenses": 1.0, "new_debt_payment": -5.0})
        with self.assertRaises(ValidationError):
            self.service.decide_review(Actor("boss", "reviewer"), record["id"], 1, "maybe")

    def test_double_decision_rejected(self):
        record = activate(self.service)
        review = self.review(record["id"], 10000.0)
        decided = self.service.decide_review(Actor("boss", "reviewer"), record["id"], review["id"], "rejected", "数据存疑")
        self.assertEqual(decided["status"], "rejected")
        with self.assertRaises(Conflict):
            self.service.decide_review(Actor("boss", "reviewer"), record["id"], review["id"], "confirmed")

    def test_reviews_survive_restart(self):
        record = activate(self.service)
        review = self.review(record["id"], 6000.0)
        self.service.decide_review(Actor("boss", "reviewer"), record["id"], review["id"], "confirmed", "")
        restarted = build_service(self.db)
        reviews = restarted.list_reviews(Actor("viewer", "reviewer"), record["id"])
        self.assertEqual(len(reviews), 1)
        self.assertEqual(reviews[0]["status"], "confirmed")
        detail = restarted.get_record(Actor("viewer", "reviewer"), record["id"])
        self.assertEqual(detail["reviews"][0]["id"], review["id"])
        timeline = restarted.timeline(Actor("viewer", "reviewer"), record["id"])
        self.assertIn("review_decided", [event["action"] for event in timeline])
