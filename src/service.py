"""业务用例编排、权限检查与审计。"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        record = self.repository.get(record_id)
        record["reviews"] = self.repository.list_reviews(record_id)
        return record

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def submit_review(self, actor: Actor, record_id: int, data: Dict[str, Any]) -> Dict[str, Any]:
        """服务人员录入复评；只新增复评记录，不改动方案状态，原方案照常履约。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_submit_review(actor.role):
            raise PermissionDenied("角色无权录入复评")
        record = self.repository.get(record_id)
        if record["state"] != "active":
            raise Conflict("仅生效中的纾困方案可以录入复评")
        data = self.rules.validate_review(data or {})
        approved_payment = float(record["payload"].get("approved_payment", 0) or 0)
        review = self.repository.add_review(
            record_id,
            data,
            actor.user_id,
            lambda last: self.rules.assess_review(approved_payment, data["monthly_income"], last),
        )
        self.audit.note(record_id, actor.user_id, "review_submitted", {
            "summary": self.rules.review_summary(review),
            "review_id": review["id"],
            "seq": review["seq"],
            "monthly_income": review["monthly_income"],
            "household_expenses": review["household_expenses"],
            "new_debt_payment": review["new_debt_payment"],
            "approved_payment": review["approved_payment"],
            "burden_ratio": review["burden_ratio"],
            "consecutive_high": review["consecutive_high"],
            "suggestion": review["suggestion"],
        })
        return review

    def list_reviews(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self.repository.get(record_id)
        return self.repository.list_reviews(record_id)

    def decide_review(self, actor: Actor, record_id: int, review_id: int, decision: Any, note: Any = "") -> Dict[str, Any]:
        """复核岗确认或驳回复评建议，结果落库并写入审计时间线。"""
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_decide_review(actor.role):
            raise PermissionDenied("角色无权复核复评")
        decision, note = self.rules.validate_review_decision(decision, note)
        review = self.repository.get_review(review_id)
        if int(review["record_id"]) != int(record_id):
            raise NotFound("复评记录不存在")
        decided = self.repository.decide_review(review_id, decision, actor.user_id, note)
        label = "确认建议" if decision == "confirmed" else "驳回建议"
        self.audit.note(record_id, actor.user_id, "review_decided", {
            "summary": "第%s次复评复核：%s（%s）" % (decided["seq"], label, self.rules.suggestion_label(decided["suggestion"])),
            "review_id": decided["id"],
            "seq": decided["seq"],
            "decision": decision,
            "suggestion": decided["suggestion"],
            "decision_note": note,
        })
        return decided

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
