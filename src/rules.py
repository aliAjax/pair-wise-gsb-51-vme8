"""住房贷款纾困申请与履约跟踪领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Optional, Tuple

from .domain import Actor, Conflict, ValidationError, boolean, choice, integer, number, optional_text, text, text_list


INITIAL_STATE = "submitted"
CREATE_ROLES = {'intake_officer'}
ACTION_ROLES = {'assess': {'intake_officer'}, 'approve': {'underwriter'}, 'activate': {'servicer'}, 'cure': {'servicer'}, 'default': {'servicer'}}
TRANSITIONS = {'assess': {'submitted': 'assessed'}, 'approve': {'assessed': 'approved'}, 'activate': {'approved': 'active'}, 'cure': {'active': 'cured'}, 'default': {'active': 'defaulted'}}

# 存续期复评：服务人员录入、复核岗确认，确认前原方案照常履约
REVIEW_SUBMIT_ROLES = {'servicer'}
REVIEW_DECIDE_ROLES = {'reviewer'}
REVIEW_HIGH_RATIO = 0.4
REVIEW_LOW_RATIO = 0.25
REVIEW_SUGGESTIONS = {'extend': '建议展期', 'exit': '建议退出纾困', 'maintain': '维持原方案'}
REVIEW_DECISIONS = ['confirmed', 'rejected']


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES) | REVIEW_SUBMIT_ROLES | REVIEW_DECIDE_ROLES
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

    def role_can_submit_review(self, role: str) -> bool:
        return role == "admin" or role in REVIEW_SUBMIT_ROLES

    def role_can_decide_review(self, role: str) -> bool:
        return role == "admin" or role in REVIEW_DECIDE_ROLES

    def validate_review(self, data: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(data or {})
        return {
            "monthly_income": number(data, "monthly_income", 0),
            "household_expenses": number(data, "household_expenses", 0),
            "new_debt_payment": number(data, "new_debt_payment", 0),
            "note": optional_text(data, "note"),
        }

    def validate_review_decision(self, decision: Any, note: Any) -> Tuple[str, str]:
        decision = choice({"decision": decision}, "decision", REVIEW_DECISIONS)
        return decision, optional_text({"note": note}, "note")

    def assess_review(self, approved_payment: float, monthly_income: float, last_review: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """按批准的月供计算承受比例，连续两次高于四成建议展期，低于两成五建议退出纾困。"""
        approved_payment = float(approved_payment)
        if monthly_income > 0:
            raw_ratio = approved_payment / monthly_income
            burden_ratio = round(raw_ratio, 4)
            high = raw_ratio > REVIEW_HIGH_RATIO
        else:
            burden_ratio = None
            high = True
        previous = int(last_review["consecutive_high"]) if last_review else 0
        consecutive_high = previous + 1 if high else 0
        if high and consecutive_high >= 2:
            suggestion = "extend"
        elif burden_ratio is not None and burden_ratio < REVIEW_LOW_RATIO:
            suggestion = "exit"
        else:
            suggestion = "maintain"
        return {"approved_payment": approved_payment, "burden_ratio": burden_ratio, "consecutive_high": consecutive_high, "suggestion": suggestion}

    def suggestion_label(self, suggestion: str) -> str:
        return REVIEW_SUGGESTIONS.get(suggestion, suggestion)

    def review_summary(self, review: Dict[str, Any]) -> str:
        if review["burden_ratio"] is None:
            ratio_text = "收入为零，承受比例按超限处理"
        else:
            ratio_text = "承受比例%.1f%%" % (float(review["burden_ratio"]) * 100)
        return "第%s次复评：%s，%s" % (review["seq"], ratio_text, self.suggestion_label(review["suggestion"]))
