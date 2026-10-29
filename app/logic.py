"""Бизнес-правила: роли, статусы, сроки, неустойка, лимиты."""
import os
from datetime import date, datetime, timedelta

ROLES = {
    "director": "Директор",
    "np": "Начальник производства",
    "rop": "РОП",
    "brigade": "Сборщик",
    "technolog": "Технолог",
    "designer": "Дизайнер",
    "supply": "Снабжение",
    "staff": "Сотрудник",
}

STATUSES = {
    "review": "На рассмотрении",
    "decided": "Решение принято",
    "waiting_consent": "Ждём согласия клиента на новый срок",
    "escalated": "Эскалация директору",
    "in_work": "В работе",
    "done": "Работы выполнены, ждём подтверждения",
    "feedback": "Ждём отзыв клиента",
    "closed": "Закрыта",
    "rejected": "Отказ (мотивированный)",
}
OPEN_STATUSES = ("review", "decided", "waiting_consent", "escalated", "in_work", "done")

DEMANDS = {
    "repair": "Безвозмездно устранить недостаток",
    "remake": "Изготовить заново / заменить",
    "price_cut": "Уменьшить цену",
    "refund": "Вернуть деньги (отказ от договора)",
    "expenses": "Возместить расходы на исправление",
}
MONEY_DEMANDS = ("price_cut", "refund", "expenses")

CONTRACT_TYPES = {"podryad": "Подряд (изготовление на заказ)", "kupla": "Купля-продажа (готовый товар)"}

CULPRITS = {
    "production": "Производство",
    "measure": "Замер",
    "design": "Дизайнер / проект",
    "technolog": "Технолог",
    "montage": "Монтаж (бригада)",
    "supplier": "Поставщик",
    "logistics": "Логистика / склад",
    "client": "Клиент / эксплуатация",
    "none": "Недостаток не подтверждён",
}

COST_KINDS = {
    "work": "Работа",
    "materials": "Материалы",
    "fittings": "Фурнитура",
    "visit": "Выезд / доставка",
    "compensation": "Компенсация клиенту",
    "penalty": "Неустойка",
}
CHARGE_TO = {
    "company": "Компания",
    "brigade": "Бригада",
    "technolog": "Технолог",
    "designer": "Дизайнер",
    "supplier": "Поставщик (претензия)",
}

REVIEW_DAYS = int(os.getenv("REVIEW_DAYS", "2"))
MAX_REPAIR_DAYS = int(os.getenv("MAX_REPAIR_DAYS", "30"))   # договорной срок устранения
MONEY_DAYS = 10                                            # ст. 22, 31 ЗоЗПП
ROP_COMPENSATION_LIMIT = float(os.getenv("ROP_COMPENSATION_LIMIT", "10000"))

# Нерабочие праздничные дни (YYYY-MM-DD через запятую) — переносят окончание срока, ст. 193 ГК
HOLIDAYS = {d.strip() for d in os.getenv("HOLIDAYS", "").split(",") if d.strip()}


def parse_date(s) -> date | None:
    if not s:
        return None
    if isinstance(s, date) and not isinstance(s, datetime):
        return s
    return datetime.fromisoformat(str(s)[:10]).date()


def roll_to_workday(d: date) -> date:
    while d.weekday() >= 5 or d.isoformat() in HOLIDAYS:
        d += timedelta(days=1)
    return d


def legal_due(received: date, demand: str) -> date:
    """Срок течёт со следующего дня после предъявления; конец в выходной переносится."""
    days = MONEY_DAYS if demand in MONEY_DEMANDS else MAX_REPAIR_DAYS
    return roll_to_workday(received + timedelta(days=days))


def review_due(received: date) -> date:
    return received + timedelta(days=REVIEW_DAYS)


def penalty(claim, today: date) -> tuple[int, float]:
    """Дней просрочки и сумма неустойки на сегодня (или на дату исполнения)."""
    due = parse_date(claim["legal_due"])
    end = parse_date(claim["done_at"]) or today
    days = (end - due).days
    if days <= 0 or not claim["contract_price"]:
        return max(days, 0), 0.0
    price = float(claim["contract_price"])
    rate = 0.03 if claim["contract_type"] == "podryad" else 0.01
    return days, round(min(price * rate * days, price), 2)


def days_left(claim, today: date) -> int:
    return (parse_date(claim["legal_due"]) - today).days


def share_used(claim, today: date) -> float:
    start = parse_date(claim["received_at"])
    due = parse_date(claim["legal_due"])
    total = max((due - start).days, 1)
    return (today - start).days / total


def traffic(claim, today: date) -> str:
    if claim["status"] in ("closed", "rejected", "feedback"):
        return "grey"
    left = days_left(claim, today)
    if left < 0:
        return "black"
    used = share_used(claim, today)
    if used >= 0.9 or (claim["plan_due"] and parse_date(claim["plan_due"]) > parse_date(claim["legal_due"])):
        return "red"
    if used >= 0.5:
        return "yellow"
    return "green"


def compensation_needs_director(amount: float) -> bool:
    return amount > ROP_COMPENSATION_LIMIT
