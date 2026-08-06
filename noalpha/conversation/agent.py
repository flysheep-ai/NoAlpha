from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable

from noalpha.conversation.events import ChatEvent, event_bus
from noalpha.conversation.models import ChatTurn, ConversationMessage
from noalpha.conversation.store import ConversationStore
from noalpha.llm.config import LLMConfig
from noalpha.llm.provider import OpenAICompatibleProvider
from noalpha.llm.schemas import LLMMessage
from noalpha.market.eastmoney_provider import EastmoneyProvider
from noalpha.market.instruments import CN_INDEX_SECIDS, INDEX_DISPLAY_NAMES, US_INDEX_SECIDS
from noalpha.pipeline import InvestigationPipeline
from noalpha.research_mode import ResearchMode

logger = logging.getLogger(__name__)

# Language instruction for prompts
LANGUAGE_INSTRUCTIONS = {
    "zh-CN": (
        "所有面向用户的分析内容必须使用简体中文撰写。保留：股票代码、公司法律名称（必要时）、"
        "证据ID（如[E12]）、数值、来源URL。不要翻译金融术语中的关键概念。"
    ),
    "en-US": (
        "All user-facing analytical prose must be written in English. "
        "Preserve: ticker symbols, company legal names when necessary, "
        "evidence IDs (e.g., [E12]), numeric values, source URLs."
    ),
}

# Tokens that look like tickers but are not
_SYMBOL_STOPWORDS = {
    "AI", "US", "USA", "SEC", "ETF", "IPO", "CEO", "CFO", "GDP", "API", "LLM",
    "OK", "PE", "PB", "EPS", "YOY", "QOQ", "A", "I", "THE", "AND", "FOR", "NOT",
}


_MARKET_QUERY_KEYWORDS = {
    "zh-CN": ["大盘", "美股", "行情", "涨跌", "走势", "市场", "指数", "跌了", "涨了", "昨天", "今天"],
    "en-US": ["market", "index", "how is the", "how did the", "how are", "indices", "spx", "dow", "nasdaq", "s&p"],
}



def _no_llm_message(lang: str) -> str:
    if lang == "zh-CN":
        return (
            "尚未配置 LLM API Key，无法生成 AI 回答。\n\n"
            "请在「设置 → LLM 模型」中填写 API Key（支持 OpenAI 兼容接口，如 DeepSeek），"
            "或设置 LLM_API_KEY 环境变量后重启后端。"
        )
    return (
        "No LLM API key is configured, so an AI answer cannot be generated.\n\n"
        "Set the API key in Settings → LLM (any OpenAI-compatible endpoint such as DeepSeek), "
        "or set the LLM_API_KEY env var and restart the backend."
    )


class ChatAgent:
    def __init__(
        self,
        store: ConversationStore,
        llm_config: LLMConfig,
        pipeline_factory: Any = None,
        llm_config_factory: Callable[[], LLMConfig] | None = None,
        investigation_lookup: Callable[[str], Any] | None = None,
    ) -> None:
        self.store = store
        self.llm_config = llm_config
        self.pipeline_factory = pipeline_factory
        self.llm_config_factory = llm_config_factory
        self.investigation_lookup = investigation_lookup
        self._cancel_events: dict[str, threading.Event] = {}

    def current_llm_config(self) -> LLMConfig:
        if self.llm_config_factory:
            return self.llm_config_factory()
        return self.llm_config

    def cancel_turn(self, turn_id: str) -> bool:
        evt = self._cancel_events.get(turn_id)
        if evt:
            evt.set()
            return True
        return False

    def run_turn(
        self,
        session: "ConversationSession",
        user_msg: ConversationMessage,
        linked_investigation_id: str = "",
        linked_claim_id: str = "",
        linked_evidence_id: str = "",
    ) -> None:
        """Run a chat turn in a background thread. Emits SSE events throughout."""
        llm_cfg = self.current_llm_config()
        turn = ChatTurn(
            session_id=session.session_id,
            user_message_id=user_msg.message_id,
            status="planning",
            started_at=user_msg.created_at,
            research_mode="llm" if llm_cfg.api_key else "deterministic",
            model=llm_cfg.model if llm_cfg.api_key else "",
        )
        self.store.create_turn(turn)
        cancel_evt = threading.Event()
        self._cancel_events[turn.turn_id] = cancel_evt

        lang = session.report_language
        lang_instruction = LANGUAGE_INSTRUCTIONS.get(lang, LANGUAGE_INSTRUCTIONS["zh-CN"])

        def _emit(etype: str, **data: Any) -> None:
            event_bus.publish(ChatEvent(
                event_type=etype,
                session_id=session.session_id,
                turn_id=turn.turn_id,
                data=data,
            ))

        def _finish(content: str, status: str = "completed") -> None:
            _emit("response_completed", content=content)
            self._save_assistant_message(session.session_id, turn.turn_id, content, lang)
            self.store.update_turn(turn.turn_id, status=status, completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"))

        try:
            # ── Step 1: Intent Detection ──
            _emit("turn_started", message="Analyzing request...")
            intent = self._detect_intent(user_msg.content, lang)
            turn.intent = intent
            self.store.update_turn(turn.turn_id, intent=intent, status="executing")
            _emit("intent_detected", intent=intent)

            if cancel_evt.is_set():
                raise InterruptedError("cancelled")

            # ── Step 2: Execute based on intent ──
            if intent == "stock_research":
                symbol = self._extract_symbol(user_msg.content)
                if symbol:
                    _emit("investigation_started", symbol=symbol, message=f"Starting investigation for {symbol}")
                    report_text = self._run_investigation(symbol, llm_cfg, session.report_language, cancel_evt, _emit)
                    _finish(report_text)
                    return

            if intent in ("bear_case_request", "bull_case_request", "risk_critique"):
                inv_id = linked_investigation_id or (session.linked_investigation_ids[0] if session.linked_investigation_ids else "")
                if inv_id:
                    _emit("tool_call_started", tool="critique", message="Analyzing existing investigation...")
                    report_text = self._critique_existing(inv_id, intent, lang, llm_cfg, lang_instruction)
                    _finish(report_text)
                    return

            if intent == "evidence_question" and linked_evidence_id:
                inv_id = linked_investigation_id or (session.linked_investigation_ids[0] if session.linked_investigation_ids else "")
                _emit("tool_call_started", tool="evidence_lookup", message="Looking up evidence...")
                report_text = self._answer_evidence_question(inv_id, linked_evidence_id, user_msg.content, lang, llm_cfg, lang_instruction)
                _finish(report_text)
                return

            if intent == "market_query":
                _emit("plan_created", message="正在获取市场数据..." if lang == "zh-CN" else "Fetching market data...")
                report_text = self._handle_market_query(user_msg.content, llm_cfg, lang, lang_instruction)
                _finish(report_text)
                return

            # ── General LLM response with conversation history ──
            if not llm_cfg.api_key:
                _finish(_no_llm_message(lang), status="failed")
                return

            _emit("plan_created", message="Generating response...")
            provider = OpenAICompatibleProvider(llm_cfg)
            messages = self._build_general_messages(session.session_id, lang_instruction)
            response = provider.generate(messages)
            _finish(response.content)

        except InterruptedError:
            _emit("turn_cancelled", message="Turn cancelled by user")
            self.store.update_turn(turn.turn_id, status="cancelled", completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        except Exception as exc:
            logger.error("Chat turn failed: %s", exc, exc_info=True)
            _emit("error", message=str(exc))
            error_text = (
                f"本轮处理失败：{exc}" if lang == "zh-CN" else f"This turn failed: {exc}"
            )
            self._save_assistant_message(session.session_id, turn.turn_id, error_text, lang, status="failed")
            self.store.update_turn(turn.turn_id, status="failed", completed_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"))
        finally:
            self._cancel_events.pop(turn.turn_id, None)

    # ── Context building ──

    def _build_general_messages(self, session_id: str, lang_instruction: str, history_limit: int = 12) -> list[LLMMessage]:
        """System prompt + recent conversation history (the latest user message is already persisted)."""
        system = LLMMessage(
            role="system",
            content=(
                "You are a financial research assistant. Ground your answers in the "
                "conversation so far; if the user refers to earlier statements "
                "(e.g. 'the second risk you mentioned'), resolve the reference from history. "
                f"{lang_instruction}"
            ),
        )
        history = self.store.get_messages(session_id, limit=50)
        recent = [m for m in history if m.role in ("user", "assistant") and m.content][-history_limit:]
        return [system] + [LLMMessage(role=m.role, content=m.content) for m in recent]

    def _detect_intent(self, content: str, lang: str) -> str:
        content_upper = content.upper()
        if any(kw in content_upper for kw in ["分析", "ANALYZE", "INVESTIGATE", "为什么跌", "为什么涨", "最近", "RECENT"]):
            if self._extract_symbol(content):
                return "stock_research"
        if any(kw in content_upper for kw in ["空头", "BEAR", "看跌", "反驳", "REFUTE", "反方"]):
            return "bear_case_request"
        if any(kw in content_upper for kw in ["多头", "BULL", "看涨", "正方"]):
            return "bull_case_request"
        if any(kw in content_upper for kw in ["证据", "EVIDENCE", "这条", "这个"]):
            return "evidence_question"
        for kw in _MARKET_QUERY_KEYWORDS.get("zh-CN", []) + _MARKET_QUERY_KEYWORDS.get("en-US", []):
            if kw in content.lower() or kw in content:
                return "market_query"
        return "general_chat"

    def _extract_symbol(self, content: str) -> str:
        for token in re.findall(r"\b[A-Z]{2,5}\b", content.upper()):
            if token not in _SYMBOL_STOPWORDS:
                return token
        return ""

    def _handle_market_query(self, query: str, llm_cfg: LLMConfig, lang: str, lang_instruction: str) -> str:
        """Detect market index query, fetch data, return a data-driven response."""
        L = lambda zh, en: zh if lang == "zh-CN" else en

        # Determine which indices to fetch based on query keywords
        q = query.lower()
        if any(kw in q for kw in ["a股", "沪指", "深成指", "创业板", "china", "shanghai", "shenzhen", "科创"]):
            targets = CN_INDEX_SECIDS
        elif any(kw in q for kw in ["us", "spx", "dow", "nasdaq", "s&p", "美股", "道指", "纳指", "标普"]):
            targets = US_INDEX_SECIDS
        elif any(kw in query for kw in ["大盘", "行情", "指数", "market", "index"]):
            # Generic market query — show US + CN indices
            targets = US_INDEX_SECIDS + CN_INDEX_SECIDS
        else:
            targets = US_INDEX_SECIDS  # default to US

        # Fetch realtime quotes via Eastmoney (same API as MarketDataGateway)
        em = EastmoneyProvider()
        try:
            quotes = em.get_quotes(targets)
        except Exception:
            quotes = []

        if not quotes:
            return L(
                "无法获取市场数据，请稍后再试。",
                "Failed to fetch market data. Please try again later.",
            )

        # Build structured results from LiveQuote objects
        results: list[dict] = []
        for qte in quotes:
            d = qte.to_dict()
            secid = d.get("secid", "")
            name_en, name_zh = INDEX_DISPLAY_NAMES.get(secid, (qte.name, qte.name))
            results.append({
                "name": name_zh if lang == "zh-CN" else name_en,
                "price": qte.price,
                "open": qte.open,
                "high": qte.high,
                "low": qte.low,
                "prev_close": qte.previous_close,
                "change": round(qte.change, 2),
                "change_pct": round(qte.change_percent, 2),
            })

        # ── Deterministic fallback (no LLM key) ──
        if not llm_cfg.api_key:
            lines = [f"## {L('市场概况', 'Market Overview')}", ""]
            for r in results:
                direction = L("涨", "up") if r["change"] >= 0 else L("跌", "down")
                emoji = "📈" if r["change"] >= 0 else "📉"
                lines.append(
                    f"{emoji} **{r['name']}**  {r['price']:.2f}  "
                    f"({direction} {abs(r['change_pct']):.2f}%, "
                    f"{direction} {abs(r['change']):.2f})"
                )
                lines.append(
                    f"  {L('昨收', 'Prev Close')}: {r['prev_close']:.2f}  "
                    f"{L('开盘', 'Open')}: {r['open']:.2f}  "
                    f"{L('最高', 'High')}: {r['high']:.2f}  "
                    f"{L('最低', 'Low')}: {r['low']:.2f}"
                )
                lines.append("")
            lines.append(
                f"_{L('数据来源：东方财富，仅供参考', 'Data source: Eastmoney, for reference only.')}_"
            )
            return "\n".join(lines)

        # ── LLM-powered response ──
        market_summary = "\n".join(
            f"- {r['name']}: price={r['price']:.2f}, prev_close={r['prev_close']:.2f}, "
            f"open={r['open']:.2f}, high={r['high']:.2f}, low={r['low']:.2f}, "
            f"change={r['change']:+.2f} ({r['change_pct']:+.2f}%)"
            for r in results
        )
        provider = OpenAICompatibleProvider(llm_cfg)
        response = provider.generate([
            LLMMessage(role="system", content=(
                "You are a financial market analyst. Answer the user's question "
                "strictly based on the provided market data. Do not invent data. "
                "If the data is insufficient to answer, say so. "
                f"{lang_instruction}"
            )),
            LLMMessage(role="user", content=(
                f"User question: {query}\n\n"
                f"Latest market data:\n{market_summary}\n\n"
                "Please answer based on this data."
            )),
        ])
        return response.content

    # ── Tools ──

    def _run_investigation(self, symbol: str, llm_cfg: LLMConfig, report_lang: str,
                           cancel_evt: threading.Event, emit) -> str:
        emit("tool_call_started", tool="investigate_stock", symbol=symbol,
             message=f"Investigating {symbol}...")
        L = (lambda zh, en: zh if report_lang == "zh-CN" else en)
        try:
            llm_active = bool(llm_cfg.api_key)
            if self.pipeline_factory:
                pipeline = self.pipeline_factory(
                    research_mode=ResearchMode.LLM if llm_active else ResearchMode.DETERMINISTIC,
                    llm_config=llm_cfg if llm_active else None,
                )
            else:
                pipeline = InvestigationPipeline.default(
                    research_mode=ResearchMode.LLM if llm_active else ResearchMode.DETERMINISTIC,
                    llm_config=llm_cfg if llm_active else None,
                )
            emit("investigation_progress", stage="fetching_data", message="Fetching market data, SEC filings, news...")
            if cancel_evt.is_set():
                raise InterruptedError("cancelled")
            result = pipeline.investigate(symbol, lookback_days=30, report_language=report_lang)
            report = result.report

            lines = [
                L(f"## {symbol} 研究报告", f"## {symbol} Research Report"),
                "",
                f"**{L('生成时间', 'Generated')}:** {report.generated_at}  **{L('截至', 'As of')}:** {report.as_of}",
                f"**{L('置信度', 'Confidence')}:** {report.confidence:.2f}"
                + ("" if llm_active else L("  （未配置 LLM，使用确定性模板引擎）", "  (no LLM key configured; deterministic template engine)")),
                "",
                f"### {L('执行摘要', 'Executive Summary')}",
                report.summary[:800],
                "",
                f"### {L('看涨观点', 'Bull Case')}",
            ]
            for line in report.bull_case[:3]:
                lines.append(f"- {line[:300]}")
            lines.append("")
            lines.append(f"### {L('看跌观点', 'Bear Case')}")
            for line in report.bear_case[:3]:
                lines.append(f"- {line[:300]}")
            lines.append("")
            lines.append(f"### {L('风险与批评', 'Risks & Critique')}")
            for line in report.risks[:3]:
                lines.append(f"- {line[:300]}")

            emit("citation_validated", claims=len(report.claims), supported=len([c for c in report.claims if c.confidence > 0.5]))
            return "\n".join(lines)
        except Exception as exc:
            emit("tool_call_failed", tool="investigate_stock", error=str(exc))
            raise

    def _load_report(self, investigation_id: str) -> dict | None:
        if not self.investigation_lookup:
            return None
        job = self.investigation_lookup(investigation_id)
        if job is None or not getattr(job, "result", None):
            return None
        return job.result

    def _critique_existing(self, investigation_id: str, intent: str, lang: str,
                           llm_cfg: LLMConfig, lang_instruction: str) -> str:
        L = (lambda zh, en: zh if lang == "zh-CN" else en)
        result = self._load_report(investigation_id)
        if not result:
            return L(
                f"找不到调查 {investigation_id} 的报告，无法分析。",
                f"Investigation {investigation_id} has no stored report to analyze.",
            )
        report = result.get("report", {})
        section = {
            "bear_case_request": ("bear_case", L("看跌观点", "Bear Case")),
            "bull_case_request": ("bull_case", L("看涨观点", "Bull Case")),
            "risk_critique": ("risks", L("风险与批评", "Risks & Critique")),
        }[intent]
        items = report.get(section[0], [])

        if not llm_cfg.api_key:
            body = "\n".join(f"- {line}" for line in items) or L("（报告中该部分为空）", "(this section of the report is empty)")
            return (
                f"### {section[1]} — {report.get('subject', {}).get('symbol', '')}\n"
                f"{body}\n\n"
                + L("（以上内容直接摘自已完成的调查报告；配置 LLM 后可获得进一步分析）",
                    "(Quoted directly from the stored report; configure an LLM for deeper analysis.)")
            )

        provider = OpenAICompatibleProvider(llm_cfg)
        prompt = (
            f"Investigation report for {report.get('subject', {}).get('symbol', '')}\n"
            f"Summary: {report.get('summary', '')[:800]}\n"
            f"Bull case: {report.get('bull_case', [])}\n"
            f"Bear case: {report.get('bear_case', [])}\n"
            f"Risks: {report.get('risks', [])}\n"
            f"Unknowns: {report.get('unknowns', [])[:6]}\n\n"
            f"User intent: {intent}. Analyze the corresponding side critically, citing the "
            "report's own evidence references (e.g. [E..]) where present. Do not invent data."
        )
        response = provider.generate([
            LLMMessage(role="system", content=f"You are a skeptical financial research critic. {lang_instruction}"),
            LLMMessage(role="user", content=prompt),
        ])
        return response.content

    def _answer_evidence_question(self, investigation_id: str, evidence_id: str, question: str,
                                  lang: str, llm_cfg: LLMConfig, lang_instruction: str) -> str:
        L = (lambda zh, en: zh if lang == "zh-CN" else en)
        result = self._load_report(investigation_id) if investigation_id else None
        item = None
        if result:
            for ev in result.get("evidence", []):
                if ev.get("evidence_id") == evidence_id:
                    item = ev
                    break
        if item is None:
            return L(
                f"未找到证据 {evidence_id}（请从调查报告的证据列表进入提问）。",
                f"Evidence {evidence_id} was not found (ask from an investigation's evidence list).",
            )

        detail = (
            f"[{item.get('evidence_id')}] type={item.get('evidence_type')} source={item.get('source')}\n"
            f"event_time={item.get('event_time')} available_at={item.get('available_at')}\n"
            f"content: {str(item.get('content'))[:600]}\n"
            f"metadata: {item.get('metadata')}"
        )
        if not llm_cfg.api_key:
            return (
                f"### {L('证据详情', 'Evidence Detail')}\n```\n{detail}\n```\n"
                + L("（LLM 未配置，仅展示原始证据；配置后可获得解读）",
                    "(No LLM configured; raw evidence shown. Configure an LLM for interpretation.)")
            )

        provider = OpenAICompatibleProvider(llm_cfg)
        response = provider.generate([
            LLMMessage(role="system", content=(
                "You answer questions about a single piece of research evidence. "
                "Use ONLY the evidence provided; if the question cannot be answered from it, say so. "
                f"{lang_instruction}"
            )),
            LLMMessage(role="user", content=f"Evidence:\n{detail}\n\nQuestion: {question}"),
        ])
        return response.content

    def _save_assistant_message(self, session_id: str, turn_id: str, content: str, lang: str,
                                status: str = "completed") -> ConversationMessage:
        msg = ConversationMessage(
            session_id=session_id, role="assistant", content=content,
            turn_id=turn_id, language=lang, status=status,
        )
        self.store.add_message(msg)
        return msg
