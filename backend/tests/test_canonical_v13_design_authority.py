from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_DESIGN = (
    REPOSITORY_ROOT / "docs/product/strategy_platform_v13_canonical_design.md"
)
HISTORICAL_DESIGN = REPOSITORY_ROOT / "docs/product/strategy_platform_v1_design.md"


def _canonical_text() -> str:
    return CANONICAL_DESIGN.read_text(encoding="utf-8")


def test_canonical_design_is_the_only_production_authority() -> None:
    canonical = _canonical_text()
    historical = HISTORICAL_DESIGN.read_text(encoding="utf-8")

    assert "FROZEN_FOR_IMPLEMENTATION" in canonical
    assert "canonical-v13-phase0-20260814" in canonical
    assert "canonical-v13-table-manifest-v1" in canonical
    assert "历史证据，不是 canonical production 设计权威" in historical
    assert "strategy_platform_v13_canonical_design.md" in historical


def test_phase_zero_freezes_exact_research_configuration_authorities() -> None:
    text = _canonical_text()
    kinds = (
        "`TARGET`",
        "`WINDOW`",
        "`GENERATION`",
        "`DIVERSITY`",
        "`QUALITY_QUALIFICATION`",
        "`SCORING`",
        "`RESEARCH_AGGREGATE`",
    )

    for kind in kinds:
        assert text.count(f"| {kind} |") == 1
    assert "independent `MARKET_DATA`" in text
    assert "production default target/count/cap | `UNSET`" in text
    assert "不存在 60/6、7、10 或其他默认值" in text


def test_required_windows_score_and_qualification_have_one_final_authority() -> None:
    text = _canonical_text()

    assert "required windows 的唯一权威" in text
    assert "score 的唯一权威" in text
    assert "qualification 的唯一权威" in text
    assert "qualifier 独占最终" in text
    assert "高分不能覆盖任一 required-window 硬门失败" in text


def test_research_and_trading_runtime_contracts_are_separate() -> None:
    text = _canonical_text()

    research = text.index("### 6.1 ephemeral research executor")
    trading = text.index("### 6.2 long-lived trading runtime")
    writers = text.index("## 5. 所有者、单写者与读者矩阵")
    assert writers < research < trading
    assert "无 credential mount、无 exchange client、无 order/risk/ledger 权限" in text
    assert "只可写 signal" in text
    assert "全系统唯一 central writer" in text


def test_phase_gates_are_ordered_without_first_backtest_cycle() -> None:
    text = _canonical_text()
    positions = [text.index(f"| #{number} ") for number in range(715, 725)]

    assert positions == sorted(positions)
    before = text.index("首次真实回测前必须完成")
    after = text.index("首次真实回测后才允许产生")
    assert before < after
    assert "schema/intake/API/UI 的验收不得反向依赖这些后置结果" in text


def test_legacy_work_is_reclassified_without_becoming_a_fallback() -> None:
    text = _canonical_text()

    for reference in ("#705 / v47", "#708", "#710", "#699", "#707"):
        assert f"| {reference} |" in text
    assert "#699/#707 保持历史不删除" in text
    assert "不得从旧 ORM、旧 migration、旧 API、旧 UI" in text
