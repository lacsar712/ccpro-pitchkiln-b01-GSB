"""相位机 / 看板一致性 / 种子覆盖的回归测试。"""
import re
from collections import Counter
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import PhaseChangeForm
from .models import CookRun, FireHearth, ResinLot, SoftPointProbe
from .seed import ensure_seed_data
from .services.floor_rules import (
    ALLOWED_PHASE_EDGES,
    change_hearth_phase,
    validate_phase_transition,
)
from .views import _board_context

ALL_PHASES = [key for key, _ in FireHearth.PHASE_CHOICES]


def _make_hearth(tag, phase, with_run=False, probes=()):
    hearth = FireHearth.objects.create(
        lane=1, tag=tag, resinGrade="特级脂", phase=phase
    )
    if with_run:
        lot = ResinLot.objects.create(
            lotCode=f"脂-测试-{tag}",
            originPlace="松脂坳东沟",
            arrivalKg=Decimal("100.00"),
            receivedAt=timezone.now(),
        )
        run = CookRun.objects.create(
            hearth=hearth,
            resinLot=lot,
            openedAt=timezone.now(),
            targetSoftPointC=Decimal("88.00"),
        )
        for value in probes:
            SoftPointProbe.objects.create(
                run=run,
                sampledAt=timezone.now(),
                softPointC=Decimal(str(value)),
                samplerName="测试员",
            )
    return hearth


class PhaseEdgeTests(TestCase):
    def test_allowed_edges_constant_is_exactly_the_five_cycle_edges(self):
        self.assertEqual(
            ALLOWED_PHASE_EDGES,
            frozenset(
                {
                    ("cold", "charging"),
                    ("charging", "ramping"),
                    ("ramping", "holding"),
                    ("holding", "drawing"),
                    ("drawing", "cold"),
                }
            ),
        )

    def test_full_cycle_passes(self):
        hearth = _make_hearth("灶-循环", "cold", with_run=True, probes=["93.00"])
        for nxt in ("charging", "ramping", "holding", "drawing", "cold"):
            change_hearth_phase(hearth, nxt)
            hearth.refresh_from_db()
            self.assertEqual(hearth.phase, nxt)

    def test_every_illegal_edge_rejected(self):
        # 即使带着合格探针，非法边也必须被边判定拦下（而非探针门槛）。
        for src in ALL_PHASES:
            for dst in ALL_PHASES:
                if (src, dst) in ALLOWED_PHASE_EDGES:
                    continue
                hearth = _make_hearth(
                    f"灶-{src}-{dst}", src, with_run=True, probes=["90.00"]
                )
                with self.assertRaises(
                    ValidationError, msg=f"{src} -> {dst} 应被拒绝"
                ):
                    validate_phase_transition(hearth, dst)
                hearth.refresh_from_db()
                self.assertEqual(hearth.phase, src)

    def test_cold_to_drawing_rejected_even_with_good_probe(self):
        hearth = _make_hearth("灶-冷直出", "cold", with_run=True, probes=["90.00"])
        with self.assertRaises(ValidationError) as ctx:
            validate_phase_transition(hearth, "drawing")
        msg = str(ctx.exception)
        self.assertIn("非法相位切换", msg)
        self.assertIn("冷灶", msg)
        self.assertIn("出胶", msg)

    def test_drawing_back_to_ramping_rejected(self):
        hearth = _make_hearth("灶-出退升", "drawing", with_run=True, probes=["90.00"])
        with self.assertRaises(ValidationError) as ctx:
            validate_phase_transition(hearth, "ramping")
        self.assertIn("非法相位切换", str(ctx.exception))

    def test_self_loop_rejected(self):
        for phase in ALL_PHASES:
            hearth = _make_hearth(f"灶-自环-{phase}", phase)
            with self.assertRaises(ValidationError, msg=f"{phase} 原地不动应被拒绝"):
                validate_phase_transition(hearth, phase)


class DrawingProbeGateTests(TestCase):
    def test_holding_to_drawing_without_open_run_rejected(self):
        hearth = _make_hearth("灶-无值守", "holding")
        with self.assertRaises(ValidationError) as ctx:
            validate_phase_transition(hearth, "drawing")
        self.assertIn("没有进行中的值守", str(ctx.exception))

    def test_holding_to_drawing_without_qualified_probe_rejected(self):
        hearth = _make_hearth(
            "灶-探针热", "holding", with_run=True, probes=["102.40", "96.20"]
        )
        with self.assertRaises(ValidationError) as ctx:
            validate_phase_transition(hearth, "drawing")
        self.assertIn("软化点探针", str(ctx.exception))

    def test_holding_to_drawing_with_qualified_probe_passes(self):
        hearth = _make_hearth(
            "灶-探针好", "holding", with_run=True, probes=["102.40", "95.00"]
        )
        change_hearth_phase(hearth, "drawing")
        hearth.refresh_from_db()
        self.assertEqual(hearth.phase, "drawing")


class PhaseFormTests(TestCase):
    def test_form_rejects_illegal_edge_in_chinese(self):
        hearth = _make_hearth("灶-表单", "cold")
        form = PhaseChangeForm({"phase": "drawing"}, hearth=hearth)
        self.assertFalse(form.is_valid())
        self.assertIn("非法相位切换", form.errors["phase"][0])

    def test_form_accepts_legal_edge(self):
        hearth = _make_hearth("灶-表单好", "cold")
        form = PhaseChangeForm({"phase": "charging"}, hearth=hearth)
        self.assertTrue(form.is_valid(), form.errors)


class ChangePhaseViewTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("viewer", password="pw123456")
        self.client.force_login(user)

    def _post_phase(self, hearth, phase, htmx=False):
        extra = {"HTTP_HX_REQUEST": "true"} if htmx else {}
        return self.client.post(
            reverse("change_phase", args=[hearth.pk]), {"phase": phase}, **extra
        )

    def test_illegal_edge_rejected_via_drawer_with_chinese_message(self):
        hearth = _make_hearth("灶-冷", "cold")
        resp = self._post_phase(hearth, "drawing")
        self.assertRedirects(resp, f"/?hearth={hearth.pk}")
        msgs = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertTrue(any("非法相位切换" in m for m in msgs), msgs)
        hearth.refresh_from_db()
        self.assertEqual(hearth.phase, "cold")

    def test_legal_edge_passes_via_drawer(self):
        hearth = _make_hearth("灶-冷2", "cold")
        self._post_phase(hearth, "charging")
        hearth.refresh_from_db()
        self.assertEqual(hearth.phase, "charging")

    def test_htmx_probe_gate_rejection_rerenders_drawer(self):
        hearth = _make_hearth("灶-保温", "holding", with_run=True, probes=["99.00"])
        resp = self._post_phase(hearth, "drawing", htmx=True)
        self.assertEqual(resp.status_code, 200)
        msgs = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertTrue(any("软化点探针" in m for m in msgs), msgs)
        hearth.refresh_from_db()
        self.assertEqual(hearth.phase, "holding")


class RunLifecycleGateTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("keeper", password="pw123456")
        self.client.force_login(user)

    def test_open_run_advances_cold_to_charging(self):
        hearth = _make_hearth("灶-开", "cold")
        lot = ResinLot.objects.create(
            lotCode="脂-开",
            originPlace="桐油坑北坡",
            arrivalKg=Decimal("10.00"),
            receivedAt=timezone.now(),
        )
        self.client.post(
            reverse("open_run", args=[hearth.pk]),
            {
                "resinLot": lot.pk,
                "openedAt": "2026-09-26T08:00",
                "targetSoftPointC": "88.00",
            },
        )
        hearth.refresh_from_db()
        self.assertEqual(hearth.phase, "charging")
        self.assertIsNotNone(hearth.open_run())

    def test_close_run_from_drawing_returns_to_cold(self):
        hearth = _make_hearth("灶-收", "drawing", with_run=True, probes=["93.00"])
        self.client.post(reverse("close_run", args=[hearth.pk]))
        hearth.refresh_from_db()
        self.assertEqual(hearth.phase, "cold")
        self.assertIsNone(hearth.open_run())

    def test_close_run_from_holding_rejected_as_illegal_edge(self):
        hearth = _make_hearth("灶-拒收", "holding", with_run=True, probes=["99.00"])
        resp = self.client.post(reverse("close_run", args=[hearth.pk]))
        msgs = [str(m) for m in get_messages(resp.wsgi_request)]
        self.assertTrue(any("非法相位切换" in m for m in msgs), msgs)
        hearth.refresh_from_db()
        self.assertEqual(hearth.phase, "holding")
        self.assertIsNotNone(hearth.open_run())


class BoardLegendTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("board", password="pw123456")
        self.client.force_login(user)
        phases = ["cold", "cold", "charging", "ramping", "holding", "drawing"]
        for i, ph in enumerate(phases):
            _make_hearth(f"灶-看板-{i}", ph)

    def test_legend_counts_equal_filtered_tile_counts(self):
        ctx = _board_context()
        legend = {key: count for key, _, count in ctx["phase_legend"]}
        tiles = Counter(h.phase for h in ctx["hearths"])
        for key, _label in FireHearth.PHASE_CHOICES:
            self.assertEqual(
                legend[key], tiles.get(key, 0), f"{key} 图例数与瓦片数不一致"
            )

    def test_rendered_legend_matches_rendered_tiles(self):
        content = self.client.get(reverse("home")).content.decode()
        for key, label in FireHearth.PHASE_CHOICES:
            m = re.search(rf'leg phase-{key}"><i></i>{label} (\d+)', content)
            self.assertIsNotNone(m, f"图例缺少 {label}")
            legend_n = int(m.group(1))
            tiles_n = content.count(f"hearth-tile phase-{key}")
            self.assertEqual(
                legend_n, tiles_n, f"{label} 图例 {legend_n} != 瓦片 {tiles_n}"
            )


class SeedTests(TestCase):
    def test_seed_covers_multiple_phases(self):
        ensure_seed_data()
        phases = set(FireHearth.objects.values_list("phase", flat=True))
        self.assertEqual(
            phases, {"cold", "charging", "ramping", "holding", "drawing"}
        )
