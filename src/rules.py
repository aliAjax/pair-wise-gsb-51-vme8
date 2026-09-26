"""住房贷款纾困申请与履约跟踪领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "submitted"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'assess': {'intake_officer'}, 'approve': {'underwriter'}, 'activate': {'servicer'}, 'cure': {'servicer'}, 'default': {'servicer'}, 'submit_review': {'servicer'}, 'decide_review': {'reviewer'}}

REVIEW_EXTEND_RATIO = 0.4
REVIEW_EXIT_RATIO = 0.25
RECOMMENDATION_LABELS = {'extend': '建议展期', 'exit': '建议退出纾困', 'none': '继续按原方案履约'}
TRANSITIONS = {'assess': {'submitted': 'assessed'}, 'approve': {'assessed': 'approved'}, 'activate': {'approved': 'active'}, 'cure': {'active': 'cured'}, 'default': {'active': 'defaulted'}}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        income = number(p, "monthly_income", 1)
        number(p, "monthly_expenses", 0)
        payment = number(p, "monthly_payment", 0)
        number(p, "arrears", 0)
        number(p, "hardship_factor", 0, 1)
        choice(p, "program_type", ["deferral", "reduction", "restructure"])
        integer(p, "requested_months", 1, 24)
        if p["monthly_expenses"] >= income:
            raise ValidationError("支出不能达到或超过收入")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        income = float(p["monthly_income"])
        disposable = income - float(p["monthly_expenses"])
        ratio = float(p["monthly_payment"]) / income
        months = min(int(p["requested_months"]), 12)
        if p["program_type"] == "deferral":
            proposed = 0.0
        elif p["program_type"] == "reduction":
            proposed = max(0.0, float(p["monthly_payment"]) - disposable * 0.4)
        else:
            proposed = max(float(p["monthly_payment"]) * 0.7, disposable * 0.25)
        p["disposable_income"] = round(disposable, 2)
        p["housing_ratio"] = round(ratio, 3)
        p["eligible_months"] = months
        p["proposed_payment"] = round(proposed, 2)
        p["risk_score"] = round(min(100.0, ratio * 60 + float(p["hardship_factor"]) * 40), 2)
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "approved", "assessed"} and item["payload"].get("borrower_id") == payload.get("borrower_id"):
                raise Conflict("该借款人已有处理中纾困申请")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "assess":
            changes["assessment_note"] = text(data, "assessment_note")
            changes["eligibility"] = bool(float(p["housing_ratio"]) <= 0.8 and float(p["arrears"]) <= float(p["monthly_payment"]) * 6)
            summary = "偿付能力评估完成"
        elif action == "approve":
            exception = boolean(data, "exception_approved")
            if not p.get("eligibility") and not exception:
                raise ValidationError("不符合纾困资格且无例外批准")
            changes["approved_program"] = p["program_type"]
            changes["approved_months"] = int(p["eligible_months"])
            changes["approved_payment"] = float(p["proposed_payment"])
            changes["exception_approved"] = exception
            summary = "纾困方案批准"
        elif action == "activate":
            if not boolean(data, "borrower_ack"):
                raise ValidationError("借款人尚未确认方案")
            changes["borrower_ack"] = True
            summary = "纾困方案生效"
        elif action == "cure":
            if not boolean(data, "arrears_cleared"):
                raise ValidationError("欠款尚未清偿")
            changes["arrears_cleared"] = True
            summary = "贷款恢复正常"
        elif action == "default":
            changes["default_reason"] = text(data, "default_reason")
            summary = "纾困方案违约"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)

    def prepare_review(self, payload: Dict[str, Any], approved_payment: float, previous_ratio: Optional[float]) -> Dict[str, Any]:
        """根据最近三个月月均收入等数据计算承受比例与复评建议。"""
        income = number(payload, "avg_monthly_income", 1)
        expenses = number(payload, "monthly_expenses", 0)
        new_debt_payment = number(payload, "new_debt_payment", 0)
        note = optional_text(payload, "note")
        approved_payment = float(approved_payment)
        ratio = approved_payment / income
        disposable = income - expenses - new_debt_payment
        if ratio < REVIEW_EXIT_RATIO:
            recommendation = "exit"
        elif ratio > REVIEW_EXTEND_RATIO and previous_ratio is not None and previous_ratio > REVIEW_EXTEND_RATIO:
            recommendation = "extend"
        else:
            recommendation = "none"
        return {
            "avg_monthly_income": round(income, 2),
            "monthly_expenses": round(expenses, 2),
            "new_debt_payment": round(new_debt_payment, 2),
            "approved_payment": round(approved_payment, 2),
            "affordability_ratio": round(ratio, 4),
            "disposable_income": round(disposable, 2),
            "recommendation": recommendation,
            "recommendation_label": RECOMMENDATION_LABELS[recommendation],
            "note": note,
        }

    def require_reviewable(self, record: Dict[str, Any]) -> None:
        if record["state"] != "active":
            raise Conflict("仅生效中的纾困方案可以复评")
        approved_payment = record["payload"].get("approved_payment")
        if approved_payment is None:
            raise Conflict("方案尚未批准月供，无法复评")
        return None

    def validate_review_decision(self, data: Dict[str, Any]) -> Tuple[str, str]:
        decision = choice(data, "decision", ["adopt", "keep"])
        note = optional_text(data, "review_note")
        return decision, note

    def review_decision_summary(self, recommendation: str, decision: str) -> str:
        if decision == "keep":
            return "复核岗确认维持原方案"
        if recommendation == "extend":
            return "复核岗采纳建议，安排展期"
        if recommendation == "exit":
            return "复核岗采纳建议，启动退出纾困"
        return "复核岗已确认复评"
