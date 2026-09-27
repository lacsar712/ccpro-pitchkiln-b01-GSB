"""灶台相位切换业务规则。

合法相位边只剩五条（循环）：

    冷灶 → 装料 → 升温 → 保温 → 出胶 → 冷灶

其余一切边（冷灶直达出胶、出胶退回升温、原地不动、跳相等）一律拒绝。
允许边与拒绝边的判定收敛在 ``assert_legal_phase_change`` 这一个函数里，
抽屉表单（PhaseChangeForm）与服务层（change_hearth_phase）都调用它，
规则不散落。
"""
from decimal import Decimal

from django.core.exceptions import ValidationError

from apps.kiln.models import FireHearth

DRAWING_SOFT_POINT_MAX = Decimal("95")

# 唯一合法相位边表：键 = 当前相位，值 = 唯一允许的下一相位。
PHASE_NEXT = {
    FireHearth.PHASE_COLD: FireHearth.PHASE_CHARGING,      # 冷灶 → 装料
    FireHearth.PHASE_CHARGING: FireHearth.PHASE_RAMPING,   # 装料 → 升温
    FireHearth.PHASE_RAMPING: FireHearth.PHASE_HOLDING,    # 升温 → 保温
    FireHearth.PHASE_HOLDING: FireHearth.PHASE_DRAWING,    # 保温 → 出胶
    FireHearth.PHASE_DRAWING: FireHearth.PHASE_COLD,       # 出胶 → 冷灶
}

PHASE_EDGE_RULE_TEXT = "合法相位边：冷灶 → 装料 → 升温 → 保温 → 出胶 → 冷灶。"

_PHASE_LABELS = dict(FireHearth.PHASE_CHOICES)


def _phase_label(phase: str) -> str:
    return _PHASE_LABELS.get(phase, phase)


def assert_can_enter_drawing(hearth) -> None:
    """
    出胶边叠加门槛：当前未收灶的 CookRun 须至少有一条
    softPointC <= 95 的 SoftPointProbe。
    """
    open_run = hearth.open_run()
    if open_run is None:
        raise ValidationError("无法进入出胶：该灶没有进行中的值守纪录。")

    ok = open_run.probes.filter(softPointC__lte=DRAWING_SOFT_POINT_MAX).exists()
    if not ok:
        raise ValidationError(
            f"无法进入出胶：进行中值守尚无软化点探针 ≤ {DRAWING_SOFT_POINT_MAX}℃。"
        )


def assert_legal_phase_change(hearth, new_phase: str) -> None:
    """
    相位切换唯一判定函数（抽屉与服务层共用）：

    1. 边合法性：只允许 PHASE_NEXT 中的五条边，其余一律中文拒绝；
    2. 出胶边（保温 → 出胶）仍叠加软化点探针门槛。
    """
    expected = PHASE_NEXT.get(hearth.phase)
    if new_phase != expected:
        raise ValidationError(
            "非法相位切换：不能由「{cur}」直接切到「{new}」。{rule}".format(
                cur=_phase_label(hearth.phase),
                new=_phase_label(new_phase),
                rule=PHASE_EDGE_RULE_TEXT,
            )
        )
    if new_phase == FireHearth.PHASE_DRAWING:
        assert_can_enter_drawing(hearth)


def change_hearth_phase(hearth, new_phase: str):
    """服务层统一入口：先过唯一判定函数，再保存。"""
    assert_legal_phase_change(hearth, new_phase)
    hearth.phase = new_phase
    hearth.save(update_fields=["phase"])
    return hearth
