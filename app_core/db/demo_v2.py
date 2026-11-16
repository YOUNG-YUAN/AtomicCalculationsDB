"""V2 demo dataset for a fresh install.

Every document is flagged ``{"demo": true}`` and can be removed from Settings.
The cards exercise all card types, the L1 master banner, conflict radar,
keywords and the evidence drawer.
"""

from __future__ import annotations

from typing import Any

from db.sqlite_manager import SQLiteManager

DEMO_TAG = {"demo": True}

DOCS: list[dict[str, Any]] = [
    {
        "doc_id": "DOC-BOK-2007-A1B2C3",
        "doc_type": "BOK",
        "title": "Relativistic Quantum Theory of Atoms and Molecules (demo excerpt)",
        "authors": ["I. P. Grant"],
        "journal": "Springer Series on Atomic, Optical, and Plasma Physics",
        "pub_year": 2007,
        "download_status": "DOWNLOADED",
        "parse_status": "LLM_EXTRACTED",
        "authority_weight": 1.5,
        "abstract": "Demo metadata inspired by the classic Grant monograph.",
        "metadata_json": {**DEMO_TAG, "note": "Demo excerpt; no source PDF attached."},
    },
    {
        "doc_id": "DOC-PAP-2022-C0FFEE",
        "doc_type": "PAP",
        "title": "MCDHF and RCI calculations for Ne-like Fe XVII (demo knowledge source)",
        "authors": ["Demo Author A", "Demo Author B"],
        "journal": "Atomic Data and Nuclear Data Tables (demo)",
        "pub_year": 2022,
        "download_status": "DOWNLOADED",
        "parse_status": "LLM_EXTRACTED",
        "authority_weight": 1.0,
        "abstract": "Demo record illustrating strategy cards and evidence bundles.",
        "metadata_json": DEMO_TAG,
    },
    {
        "doc_id": "DOC-MAN-2016-BADA55",
        "doc_type": "MAN",
        "title": "GRASP2K user's guide (demo excerpt)",
        "authors": ["Demo Author C"],
        "journal": "Computer Physics Communications (demo)",
        "pub_year": 2016,
        "download_status": "DOWNLOADED",
        "parse_status": "LLM_EXTRACTED",
        "authority_weight": 1.2,
        "abstract": "Demo excerpt of a program manual.",
        "metadata_json": {**DEMO_TAG, "note": "Demo excerpt of a program manual."},
    },
    {
        "doc_id": "DOC-PAP-2024-DEAD01",
        "doc_type": "PAP",
        "title": "Forbidden transitions in highly charged ions: a demo manual-download item",
        "authors": ["Demo Author D"],
        "journal": "Physical Review A (demo)",
        "pub_year": 2024,
        "download_status": "MANUAL_REQUIRED",
        "parse_status": "PENDING",
        "authority_weight": 1.0,
        "metadata_json": {**DEMO_TAG, "note": "Demonstrates the manual download queue."},
    },
]


def seed_demo_v2(db: SQLiteManager) -> dict[str, int]:
    doc_ids: dict[str, str] = {}
    for doc in DOCS:
        doc_ids[doc["doc_id"]] = db.upsert_document(doc)

    grant = doc_ids["DOC-BOK-2007-A1B2C3"]
    paper = doc_ids["DOC-PAP-2022-C0FFEE"]
    grasp = doc_ids["DOC-MAN-2016-BADA55"]

    cards: list[dict[str, Any]] = [
        {
            "card_level": "L1",
            "card_type": "MASTER",
            "title_zh": "类氖等电子序列 MCDHF / RCI 总纲",
            "title_en": "Master overview: MCDHF/RCI for Ne-like ions",
            "summary_zh": "汇总类氖离子 ^1S_0 基态的 MCDHF/RCI 活性空间扩展、CV 关联、Breit/QED 与规范收敛策略。",
            "premises_and_conditions": "适用于 Ne-like 高电荷态离子；低 Z 体系需额外检查电子关联截断。",
            "isoelectronic": "Ne-like",
            "method": "MCDHF+RCI",
            "element": "Fe",
            "z_num": 26,
            "ion_stage": "Fe XVII",
            "physical_process": "E1_transition",
            "conclusion_flags": 1 | 2 | 8,
            "supporting_doc_ids": [paper, grant],
            "authority_score": 1.5,
            "consensus_count": 6,
            "recency_year": 2022,
            "search_payload": (
                "MASTER Ne-like MCDHF RCI overview active space core-valence "
                "Breit QED gauge convergence Fe XVII"
            ),
        },
        {
            "card_level": "L2",
            "card_type": "STRAT",
            "title_zh": "Fe XVII 的 MCDHF + RCI 分层计算策略",
            "title_en": "Layered MCDHF+RCI strategy for Fe XVII",
            "summary_zh": "**核心策略**：以 [Ne] 2s^2 2p^6 为参考态，逐层打开 2s/2p 芯层，活性空间扩展至 n=7，再用 RCI 处理 ^3P_2 等跃迁与电子关联。",
            "premises_and_conditions": "适用于 Ne-like Fe XVII；必须在 length/velocity 两种规范下检查收敛，并在 RCI 阶段加入 Breit。",
            "element": "Fe",
            "z_num": 26,
            "ion_stage": "Fe XVII",
            "isoelectronic": "Ne-like",
            "shell_type": "closed-shell",
            "method": "MCDHF+RCI",
            "code_name": "GRASP2018",
            "physical_process": "E1_transition",
            "conclusion_flags": 1 | 2 | 8,
            "structured_details_json": {
                "reference_config": "[Ne] 2s2 2p6",
                "active_space": "n=3..7, l<=3",
                "correlation": "CV + CC",
                "breit": True,
                "gauge_check": "length vs velocity",
            },
            "supporting_doc_ids": [paper],
            "authority_score": 1.2,
            "consensus_count": 4,
            "recency_year": 2022,
            "source_section": "2. Computational details",
            "page_num": 4,
            "search_payload": (
                "Fe XVII MCDHF RCI strategy reference configuration Ne 2s2 2p6 "
                "active space n=7 core-valence Breit gauge convergence E1"
            ),
        },
        {
            "card_level": "L2",
            "card_type": "MODEL",
            "title_zh": "MCDHF 与 Dirac–Coulomb–Breit 模型卡",
            "title_en": "MCDHF and Dirac-Coulomb-Breit model",
            "summary_zh": "多组态 Dirac–Hartree–Fock 自洽优化径向轨道，RCI 在固定轨道基上处理关联与跃迁；Breit/QED 作微扰修正。",
            "premises_and_conditions": "适用于中 Z 高电荷态离子；有限核模型与 QED 微扰是前置近似。",
            "element": "Fe",
            "z_num": 26,
            "ion_stage": "Fe XVII",
            "isoelectronic": "Ne-like",
            "method": "MCDHF+RCI",
            "code_name": "GRASP2018",
            "physical_process": "energy_levels",
            "conclusion_flags": 4 | 8,
            "formulas_latex": r"H = \sum_i h_D(i) + \sum_{i<j}\left(\frac{1}{r_{ij}}+B_{ij}\right)",
            "structured_details_json": {
                "hamiltonian": "Dirac-Coulomb-Breit",
                "approximations": ["finite nucleus", "Breit perturbative", "QED perturbative"],
                "eol": "extended optimal level",
            },
            "supporting_doc_ids": [grasp, grant],
            "authority_score": 1.5,
            "consensus_count": 5,
            "recency_year": 2016,
            "source_section": "3. Model and approximations",
            "page_num": 8,
            "search_payload": (
                "MCDHF Dirac-Coulomb-Breit Hamiltonian RCI radial orbitals finite nucleus "
                "Breit QED perturbative extended optimal level"
            ),
        },
        {
            "card_level": "L2",
            "card_type": "MODEL",
            "title_zh": "相对论多体微扰理论（RMBPT）模型卡",
            "title_en": "Relativistic many-body perturbation theory model",
            "summary_zh": "以 Dirac–Coulomb–Breit 为零阶、按 1/Z 展开电子关联；适合高 Z 少电子体系，二/三阶截断需系统检查。",
            "premises_and_conditions": "适用于高电荷态少电子离子；低 Z 多电子体系收敛慢，需转 MCDHF 或 CI+MBPT。",
            "element": "W",
            "z_num": 74,
            "ion_stage": "W LXXIV",
            "shell_type": "few-electron",
            "method": "RMBPT",
            "physical_process": "energy_levels",
            "conclusion_flags": 4 | 16,
            "formulas_latex": r"B_{ij}=-\frac{\boldsymbol{\alpha}_i\cdot\boldsymbol{\alpha}_j}{2r_{ij}}-\frac{(\boldsymbol{\alpha}_i\cdot\mathbf{r}_{ij})(\boldsymbol{\alpha}_j\cdot\mathbf{r}_{ij})}{2r_{ij}^3}",
            "structured_details_json": {
                "zero_order": "Dirac-Fock",
                "expansion": "1/Z",
                "truncation": "second/third order",
                "breit": "alpha^2 order",
            },
            "supporting_doc_ids": [grant],
            "authority_score": 1.5,
            "consensus_count": 3,
            "recency_year": 2007,
            "source_section": "8.4 Many-body perturbation theory",
            "page_num": 212,
            "search_payload": (
                "RMBPT high-Z tungsten Dirac-Coulomb-Breit 1/Z expansion second third order "
                "Brueckner orbitals energy levels"
            ),
        },
        {
            "card_level": "L2",
            "card_type": "CODE",
            "title_zh": "GRASP2K 标准执行工作流",
            "title_en": "Standard GRASP2K workflow",
            "summary_zh": "从组态列表生成 CSF，依次运行 `rwfnestimate`、`rmcdhf`、`rci`，最后用 `rtransition` 输出跃迁数据。",
            "premises_and_conditions": "适用于 GRASP2K/GRASP2018；CSF 数量需控制在可用内存内。",
            "element": "Fe",
            "z_num": 26,
            "ion_stage": "Fe XVII",
            "method": "MCDHF+RCI",
            "code_name": "GRASP2018",
            "physical_process": "E1_transition",
            "conclusion_flags": 8 | 32,
            "code_snippet": "rncsl --configs csl.inp\nrwfnestimate < rwfnestimate.inp\nrmcdhf < rmcdhf.inp\nrci < rci.inp\nrtransition < rtransition.inp",
            "structured_details_json": {
                "program": "GRASP2K",
                "workflow": ["rncsl", "rwfnestimate", "rmcdhf", "rci", "rtransition"],
                "csf_limit": 1000000,
            },
            "supporting_doc_ids": [grasp],
            "authority_score": 1.2,
            "consensus_count": 2,
            "recency_year": 2016,
            "source_section": "4. Running the codes",
            "page_num": 22,
            "search_payload": (
                "GRASP2K workflow rncsl rwfnestimate rmcdhf rci rtransition input "
                "CSF active space Fe XVII code template"
            ),
        },
        {
            "card_level": "L2",
            "card_type": "CODE",
            "title_zh": "FAC 快速组态相互作用工作流",
            "title_en": "FAC rapid configuration-interaction workflow",
            "summary_zh": "FAC 以组态平均方法快速生成原子数据，适合大规模谱线表与等离子体建模。",
            "premises_and_conditions": "适合快速、可扩展的原子数据生产；精度通常低于逐层 MCDHF，高 Z 体系需检查 Breit/QED 选项。",
            "method": "FAC",
            "code_name": "FAC",
            "physical_process": "oscillator_strength",
            "conclusion_flags": 8 | 32,
            "code_snippet": "fac fac.inp\nfac -d fac.inp",
            "structured_details_json": {
                "program": "FAC",
                "workflow": ["prepare fac.inp", "run fac", "inspect levels", "export"],
            },
            "supporting_doc_ids": [paper],
            "authority_score": 1.0,
            "consensus_count": 1,
            "recency_year": 2022,
            "source_section": "3. FAC setup",
            "page_num": 11,
            "search_payload": (
                "FAC configuration interaction workflow collisional data oscillator strength "
                "plasma modeling atomic data"
            ),
        },
        {
            "card_level": "L2",
            "card_type": "PITFALL",
            "title_zh": "长度/速度规范差异与变分崩塌排错",
            "title_en": "Gauge discrepancy and variational collapse troubleshooting",
            "summary_zh": "若 length/velocity 规范结果差异显著，优先检查活性空间对称性、轨道优化与变分崩塌，而不是直接平均两套结果。",
            "premises_and_conditions": "适用于 MCDHF/RCI 的 E1 跃迁检查；规范差异是诊断工具，不是可忽略的数值噪声。",
            "element": "Fe",
            "z_num": 26,
            "ion_stage": "Fe XVII",
            "isoelectronic": "Ne-like",
            "method": "MCDHF+RCI",
            "physical_process": "E1_transition",
            "conclusion_flags": 16 | 32,
            "pitfall_warning": "检查活性空间是否按 l 对称扩展；若高激发态塌缩到连续态，需加约束或改用 RCI。",
            "structured_details_json": {
                "symptom": "length/velocity gauge discrepancy",
                "checks": [
                    "active-space symmetry",
                    "orbital convergence",
                    "variational collapse",
                    "Breit/QED consistency",
                ],
            },
            "supporting_doc_ids": [grant, paper],
            "authority_score": 1.5,
            "consensus_count": 3,
            "recency_year": 2007,
            "source_section": "12.6 Gauge considerations",
            "page_num": 356,
            "search_payload": (
                "gauge discrepancy length velocity variational collapse intruder state "
                "active space symmetry E1 transition troubleshooting"
            ),
        },
        {
            "card_level": "L2",
            "card_type": "CONC",
            "title_zh": "规范收敛是 E1 计算的最低验收条件",
            "title_en": "Gauge convergence as the minimum acceptance criterion",
            "summary_zh": "当 length 与 velocity 规范在 1% 内一致时，才认为活性空间与轨道优化已达到可用于生产的收敛水平。",
            "premises_and_conditions": "前提是两套规范使用同一哈密顿量与同一组态空间；否则差异不能用于判断收敛。",
            "element": "Fe",
            "z_num": 26,
            "ion_stage": "Fe XVII",
            "isoelectronic": "Ne-like",
            "method": "MCDHF+RCI",
            "physical_process": "E1_transition",
            "conclusion_flags": 1 | 2 | 8,
            "supporting_doc_ids": [paper],
            "authority_score": 1.2,
            "consensus_count": 2,
            "recency_year": 2022,
            "source_section": "3. Results",
            "page_num": 9,
            "search_payload": (
                "length velocity gauge agreement within 1 percent convergence acceptance "
                "E1 transition Fe XVII"
            ),
        },
        {
            "card_level": "L1",
            "card_type": "MASTER",
            "title_zh": "高电荷态离子 RMBPT 总纲",
            "title_en": "Master overview: RMBPT for highly charged ions",
            "summary_zh": "高 Z 少电子体系优先使用 RMBPT；以 Brueckner 轨道与二/三阶截断控制高阶关联，并与 MCDHF 交叉验证。",
            "premises_and_conditions": "适用于 W、U 等高 Z 少电子离子；低 Z 体系改用 MCDHF/CI+MBPT。",
            "isoelectronic": "high-Z",
            "method": "RMBPT",
            "element": "W",
            "z_num": 74,
            "ion_stage": "W LXXIV",
            "physical_process": "energy_levels",
            "conclusion_flags": 2 | 16 | 32,
            "supporting_doc_ids": [grant],
            "authority_score": 1.5,
            "consensus_count": 3,
            "recency_year": 2007,
            "search_payload": "MASTER high-Z RMBPT tungsten Brueckner truncation MCDHF cross check",
        },
    ]

    card_ids: list[str] = []
    for card in cards:
        card_ids.append(db.upsert_knowledge_card(card))

    # Evidence bundles (L3) for the drawer.
    evidence = [
        (card_ids[1], paper, "⟪FOCUS⟫ The 2s and 2p shells are opened layer by layer, and the active space is expanded up to n=7. The length and velocity gauges agree within 1%. ⟪/FOCUS⟫", "2. Computational details", 4),
        (card_ids[2], grasp, "⟪FOCUS⟫ MCDHF optimises the radial orbitals; RCI then diagonalises the Hamiltonian in the fixed orbital basis. ⟪/FOCUS⟫", "3. Model", 8),
        (card_ids[3], grant, "⟪FOCUS⟫ The RMBPT series is truncated at second or third order in the residual interaction. ⟪/FOCUS⟫", "8.4 RMBPT", 212),
        (card_ids[4], grasp, "⟪FOCUS⟫ Run rwfnestimate, rmcdhf, rci and rtransition in sequence. ⟪/FOCUS⟫", "4. Running the codes", 22),
        (card_ids[5], paper, "⟪FOCUS⟫ The FAC workflow is intended for rapid large-scale atomic-data production. ⟪/FOCUS⟫", "3. FAC setup", 11),
        (card_ids[6], grant, "⟪FOCUS⟫ A large length/velocity discrepancy usually indicates incomplete active-space symmetry or variational collapse. ⟪/FOCUS⟫", "12.6 Gauge considerations", 356),
        (card_ids[7], paper, "⟪FOCUS⟫ Agreement within 1% between gauges is used as the acceptance criterion. ⟪/FOCUS⟫", "3. Results", 9),
    ]
    for knw_id, doc_id, text, section, page in evidence:
        db.add_evidence(knw_id, doc_id, text, section_title=section, page_num=page)

    keyword_terms = [
        ("core-valence correlation", "芯价关联", ["CV correlation"], "CORRELATION"),
        ("Breit interaction", "布雷特相互作用", ["Breit"], "QED_REL"),
        ("kinetic balance", "动能平衡", ["kinetic balance condition"], "BASIS"),
        ("MCDHF", "多组态狄拉克-哈特里-福克", ["multiconfiguration Dirac-Hartree-Fock"], "METHOD"),
        ("RCI", "相对论组态相互作用", ["relativistic configuration interaction"], "METHOD"),
        ("RMBPT", "相对论多体微扰理论", ["relativistic many-body perturbation theory"], "METHOD"),
        ("active space", "活性空间", ["correlation space"], "CONVERGENCE"),
        ("variational collapse", "变分崩塌", ["variational collapse"], "CONVERGENCE"),
        ("gauge convergence", "规范收敛", ["length-velocity gauge"], "CONVERGENCE"),
        ("extended optimal level", "扩展最优能级", ["EOL"], "CONVERGENCE"),
    ]
    keyword_ids: list[str] = []
    for term, zh, synonyms, category in keyword_terms:
        keyword_ids.append(
            db.upsert_keyword(term, term_zh=zh, synonyms=synonyms, category=category)
        )
    links = {
        card_ids[0]: [3, 4, 0, 1, 6, 8],
        card_ids[1]: [3, 4, 0, 1, 6, 8],
        card_ids[2]: [3, 1, 9, 6],
        card_ids[3]: [5, 1, 6],
        card_ids[4]: [3, 4, 9],
        card_ids[7]: [8, 6, 2],
    }
    for knw_id, indices in links.items():
        db.link_card_keywords(knw_id, [keyword_ids[index] for index in indices])
    db.set_meta("demo_keyword_ids", ",".join(keyword_ids))

    # A small queue so the Night view is populated.
    if not db.list_tasks("PENDING"):
        db.add_task(
            "DISCOVER",
            payload={"query": "Fe XVII MCDHF active space", "demo": True},
            priority=4,
        )
        db.add_task(
            "SYNC", payload={"provider": "webdav", "demo": True}, priority=1
        )

    db.log_event("INFO", "demo", "已载入 V2 示例数据")
    return {
        "documents": len(DOCS),
        "cards": len(cards),
        "keywords": len(keyword_terms),
        "evidence": len(evidence),
    }
