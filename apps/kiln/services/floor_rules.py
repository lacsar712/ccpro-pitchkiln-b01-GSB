"""灶台相位切换业务规则。"""
from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.kiln.models import FireHearth

DRAWING_SOFT_POINT_MAX = Decimal("95")

# 合法相位流（单向循环）：冷灶 → 装料 → 升温 → 保温 → 出胶 → 冷灶
PHASE_FLOW = [
    FireHearth.PHASE_COLD,
    FireHearth.PHASE_CHARGING,
    FireHearth.PHASE_RAMPING,
    FireHearth.PHASE_HOLDING,
    FireHearth.PHASE_DRAWING,
]

# 合法相位边：仅以上循环的 5 条邻接边，其余一律拒绝。
ALLOWED_PHASE_EDGES = frozenset(
    zip(PHASE_FLOW, PHASE_FLOW[1:] + PHASE_FLOW[:1])
)

_PHASE_LABELS = dict(FireHearth.PHASE_CHOICES)

ALLOWED_EDGES_TEXT = " → ".join(
    _PHASE_LABELS[p] for p in PHASE_FLOW + PHASE_FLOW[:1]
)


def assert_can_enter_drawing(hearth) -> None:
    """
    进入「出胶」相位前：当前未收灶的 CookRun 须至少有一条
    softPointC <= 95 的 SoftPointProbe。
    """
    open_run = hearth.open_run()
    if open_run is None:
        raise ValidationError("无法进入出胶：该灶没有进行中的值守纪录。")

    ok = open_run.probes.filter(softPointC__lte=DRAWING_SOFT_POINT_MAX).exists()
    if not ok:
        raise ValidationError(
            "无法进入出胶：进行中值守尚无软化点探针 "
            f"≤ {DRAWING_SOFT_POINT_MAX}℃。"
        )


def validate_phase_transition(hearth, new_phase: str) -> None:
    """
    单一判定函数：相位边合法性 + 出胶探针门槛。

    抽屉表单（PhaseChangeForm）与服务层（change_hearth_phase，
    含开灶 / 收灶的相位联动）都必须调用它；
    非法切换抛出中文 ValidationError，合法则静默通过。
    """
    if (hearth.phase, new_phase) not in ALLOWED_PHASE_EDGES:
        raise ValidationError(
            "非法相位切换："
            f"{_PHASE_LABELS.get(hearth.phase, hearth.phase)} → "
            f"{_PHASE_LABELS.get(new_phase, new_phase)}。"
            f"仅允许：{ALLOWED_EDGES_TEXT}。"
        )

    # 出胶边（保温 → 出胶）仍叠加软化点探针门槛。
    if new_phase == FireHearth.PHASE_DRAWING:
        assert_can_enter_drawing(hearth)


def change_hearth_phase(hearth, new_phase: str):
    """统一入口：所有相位变更都经单一判定函数校验后保存。"""
    validate_phase_transition(hearth, new_phase)

    hearth.phase = new_phase
    hearth.save(update_fields=["phase"])
    return hearth
