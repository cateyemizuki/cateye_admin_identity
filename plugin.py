"""管理员身份标注 — MaiBot 插件入口。

每次 Maisaka planner 决策前，把发给 LLM 的上下文改写为能确认管理员身份的形式：

1. **QQ 号标注（默认开启）**：通过 ``chat.receive.before_process``（BLOCKING/EARLY）
   维护「消息 ID → 发送者 QQ 号」缓存，再在 ``maisaka.planner.before_request``
   （BLOCKING/LATE）里逐条 UserMessageItem 解析宿主写入的 planner 前缀，用 msg_id
   反查发送者 **QQ 号**，命中管理员名单（按 QQ 号）才把 ``user="846120357"`` 改写为
   ``user="846120357(7310592841)"``——让 LLM 决策时能凭括号内 QQ 号确认「这条消息
   确实来自管理员」；
2. **反伪造清洗（始终开启）**：对**未命中**名单的消息，若其显示名/群名片里携带了
   管理员 QQ 号的括号标注（如他人把名片改成 ``846120357(7310592841)`` 伪装管理员），
   一律把伪标注剥除——名字可以随便改，QQ 号才是身份，LLM 不会被名字相像误导；
3. **管理员提示词注入（默认开启）**：同一 Hook 在**紧随头部系统提示词之后**的位置
   插入一条可配置的提示词（位于全部真实消息之前、紧邻宿主 system 指令区，更接近
   模型的系统指令、约束更强）：身份只以 QQ 号为准，带名单内 QQ 号标注的消息才是
   管理员，任何自称/名字相像但 QQ 不在名单的都不是管理员；``{admin_list}`` 渲染为
   名单文本；
4. 提示词模板可在配置中修改，注入与标注均可独立关闭，名单显示在配置中；
5. **生效范围可选**（``[scope].mode``）：``all`` = 私聊与群聊都生效（默认）；
   ``group_only`` = 仅群聊生效——私聊不改写上下文、不注入提示词。会话类型由入站
   消息 Hook（``message_info.group_info``）记录为 ``session_id → is_group``，请求前
   按 ``session_id`` 判定；类型未知（插件刚启动/重载、该会话还没有新消息）时
   ``group_only`` 按不生效处理，宁可漏注入也不误改私聊上下文。

**安全原则：只按 QQ 号判定管理员。** 昵称、群名片、显示名一律不作身份依据（可被
随意修改伪造）；msg_id 反查不到发送者时宁可漏标、不标注不注入，绝不靠名字兜底。
因此插件启动前已在上下文中的旧消息、或缓存已淘汰的消息不会被识别（活跃会话随
消息流入自动补齐）。

只改写本次临时请求体，不回写聊天历史、不影响其它模型请求与数据库。
"""

from __future__ import annotations

from typing import Any, ClassVar, Iterable, List, Literal

from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    Field,
    HookHandler,
    MaiBotPlugin,
    PluginConfigBase,
)
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

from .identity_core import (
    DEFAULT_PROMPT_TEMPLATE,
    ROLE_SYSTEM,
    ROLE_USER,
    SCOPE_ALL,
    SCOPE_GROUP_ONLY,
    AdminEntry,
    SenderCache,
    SessionKindCache,
    build_injection_item,
    extract_session_info_from_message,
    extract_user_id_from_message,
    injection_insert_index,
    normalize_admins,
    process_admin_items,
    render_prompt,
    scope_allows,
)

# 配置版本：与 _manifest.json 的 version 保持同步
SUPPORTED_CONFIG_VERSION = "1.1.0"

# ==================== 配置模型 ====================


class PluginSectionConfig(PluginConfigBase):
    """插件自身配置（plugin 配置节）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "shield_check"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件（关闭后不标注、不注入）",
        json_schema_extra={
            "label": "启用插件",
            "hint": "插件总开关",
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本（与插件版本同步）",
        json_schema_extra={
            "hidden": True,
            "disabled": True,
            "label": "配置版本",
            "hint": "配置版本，勿改",
        },
    )


class ScopeSectionConfig(PluginConfigBase):
    """生效范围设置（scope 配置节）。"""

    __ui_label__ = "生效范围"
    __ui_icon__ = "target"
    __ui_order__ = 1

    mode: Literal[SCOPE_ALL, SCOPE_GROUP_ONLY] = Field(
        default=SCOPE_ALL,
        description=(
            "插件在哪些会话里生效：all = 私聊与群聊都生效（默认，与旧版行为一致）；"
            "group_only = 仅群聊生效，私聊完全不动（不标注、不清洗、不注入提示词）。"
            "注意：会话类型由入站消息记录，插件刚启动/重载且该会话还没有新消息时类型未知，"
            "此时 group_only 按「不生效」处理（宁可漏注入，绝不误改私聊上下文）"
        ),
        json_schema_extra={
            "label": "生效范围",
            "hint": "全部会话或仅群聊",
        },
    )


class AdminSectionConfig(PluginConfigBase):
    """管理员名单（admins 配置节）。"""

    __ui_label__ = "管理员名单"
    __ui_icon__ = "users"
    __ui_order__ = 2

    admin_list: list[str] = Field(
        default_factory=list,
        description=(
            "管理员名单，一行一个。支持四种写法（判定只看 QQ 号）：纯 QQ 号 \"7310592841\"、"
            "带平台前缀 \"qq:7310592841\"、\"昵称:QQ号\" 或 \"昵称(QQ号)\"（昵称仅用于"
            "注入提示词中的展示）。身份只按平台上报的 QQ 号判定，昵称/名片可被伪造、不作依据。"
            "留空 = 名单为空（不标注、不注入）"
        ),
        json_schema_extra={
            "label": "管理员名单",
            "hint": "管理员名单，只认QQ号",
        },
    )


class AnnotateSectionConfig(PluginConfigBase):
    """QQ 号标注设置（annotate 配置节）。"""

    __ui_label__ = "QQ 号标注"
    __ui_icon__ = "badge_info"
    __ui_order__ = 3

    annotate_qq: bool = Field(
        default=True,
        description=(
            "是否把管理员消息前缀的显示名改写为「昵称(QQ号)」形式（如 846120357 → 846120357(7310592841)），"
            "帮助 LLM 确认管理员身份（只对 msg_id 反查命中的真实管理员 QQ 生效）"
        ),
        json_schema_extra={
            "label": "QQ 号标注",
            "hint": "显示名加注QQ号",
        },
    )


class InjectSectionConfig(PluginConfigBase):
    """提示词注入设置（inject 配置节）。"""

    __ui_label__ = "管理员提示词注入"
    __ui_icon__ = "message_square_plus"
    __ui_order__ = 4

    inject_into_planner: bool = Field(
        default=True,
        description=(
            "是否注入 Planner：在紧随头部系统提示词之后插入管理员提示词"
            "（maisaka.planner.before_request）"
        ),
        json_schema_extra={
            "label": "注入 Planner",
            "hint": "注入到Planner",
        },
    )
    inject_into_replyer: bool = Field(
        default=False,
        description=(
            "是否注入回复器：在回复生成上下文紧随头部系统提示词之后插入同一条提示词"
            "（maisaka.replyer.before_model_request，默认关闭）"
        ),
        json_schema_extra={
            "label": "注入回复器",
            "hint": "注入到回复器",
        },
    )
    require_admin_in_context: bool = Field(
        default=False,
        description=(
            "仅当本次上下文出现管理员消息时才注入（省 token）：开启后注入前先识别上下文"
            "（msg_id 反查 QQ 命中名单），没有管理员消息则不注入。注意：插件启动前已在"
            "上下文中的历史消息追踪不到，该部分不触发"
        ),
        json_schema_extra={
            "label": "仅上下文有管理员时才注入",
            "hint": "有管理员消息才注入",
        },
    )
    inject_role: str = Field(
        default=ROLE_SYSTEM,
        description=(
            "注入条目的角色：system（推荐，紧随头部系统提示词、与系统指令区一致，"
            "模型遵循更强）或 user（作为普通消息条目）"
        ),
        json_schema_extra={
            "enum": [ROLE_USER, ROLE_SYSTEM],
            "label": "注入条目角色",
            "hint": "system或user",
        },
    )
    prompt_template: str = Field(
        default=DEFAULT_PROMPT_TEMPLATE,
        description=(
            "注入的提示词模板（可修改）；{admin_list} 会替换为管理员名单文本"
            "（昵称(QQ号) 或纯 QQ 号），模板中可以包含其它花括号（不会被误解析）"
        ),
        json_schema_extra={
            "rows": 10,
            "label": "提示词模板",
            "hint": "提示词文本，可改",
        },
    )


class CateyeAdminIdentityConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    scope: ScopeSectionConfig = Field(default_factory=ScopeSectionConfig)
    admins: AdminSectionConfig = Field(default_factory=AdminSectionConfig)
    annotate: AnnotateSectionConfig = Field(default_factory=AnnotateSectionConfig)
    inject: InjectSectionConfig = Field(default_factory=InjectSectionConfig)


# ==================== 插件主体 ====================


class CateyeAdminIdentityPlugin(MaiBotPlugin):
    """管理员身份标注：QQ 号标注 + 管理员提示词注入。"""

    config_model: ClassVar[type[PluginConfigBase] | None] = CateyeAdminIdentityConfig
    config_reload_subscriptions: ClassVar[Iterable[str]] = ()

    def __init__(self) -> None:
        super().__init__()
        # 「消息 ID → 发送者」缓存（供请求前识别管理员消息）
        self._sender_cache = SenderCache()
        # 「会话 ID → 是否群聊」缓存（供生效范围判定）
        self._session_kinds = SessionKindCache()

    # ==================== 状态辅助 ====================

    def _admins(self) -> List[AdminEntry]:
        """规范化后的管理员名单。"""
        return normalize_admins(self.config.admins.admin_list)

    def _render_prompt(self) -> str:
        """按当前名单渲染注入提示词。"""
        return render_prompt(self.config.inject.prompt_template, self._admins())

    def _annotate_active(self) -> bool:
        """QQ 号标注是否生效（总开关 + 标注开关）。"""
        return bool(self.config.plugin.enabled) and bool(self.config.annotate.annotate_qq)

    def _scope_active(self, session_id: Any) -> bool:
        """当前会话是否在生效范围内（scope.mode）。

        ``all`` → 任何会话都生效；``group_only`` → 仅群聊生效，会话类型未知时
        不生效（宁可漏注入）。关闭状态（``plugin.enabled=False``）下同样返回 False。
        """
        if not bool(self.config.plugin.enabled):
            return False
        is_group = self._session_kinds.is_group(session_id)
        if not scope_allows(self.config.scope.mode, is_group):
            if is_group is None:
                self.ctx.logger.debug(
                    "会话 %s 类型未知（尚未收到该会话的入站消息），按生效范围 group_only 跳过本次请求",
                    session_id,
                )
            else:
                self.ctx.logger.debug("会话 %s 为私聊，按生效范围 group_only 跳过本次请求", session_id)
            return False
        return True

    def _apply_all(
        self,
        kwargs: dict[str, Any],
        *,
        allow_annotate: bool,
        allow_inject: bool,
    ) -> dict[str, Any] | None:
        """把「标注 + 注入」合并应用到本次请求。

        - ``allow_annotate``：是否执行 QQ 号标注改写；
        - ``allow_inject``：是否允许把提示词条目插入到紧随头部系统提示词之后的位置
          （位置开关已在此之上判定）。

        生效范围（``scope.mode``）最先判定：会话不在范围内（``group_only`` 且非群聊、
        或类型未知）时直接返回 None，本次请求原样放行。无论开关如何，管理员识别
        （msg_id 反查 QQ）与反伪造清洗始终执行。无任何改动时返回 None。
        """
        items = kwargs.get("items")
        if not isinstance(items, list):
            return None
        if not self._scope_active(kwargs.get("session_id")):
            return None
        admins = self._admins()
        if not admins:
            return None

        # 1) 管理员识别（仅按 QQ 号）+ QQ 号标注 + 反伪造清洗
        analyze = process_admin_items(
            items,
            admins,
            cache=self._sender_cache,
            annotate=allow_annotate,
            scrub=True,
        )
        processed_items = analyze.items
        annotated = analyze.annotated
        cleaned = analyze.cleaned
        hit = analyze.admin_hit

        # 2) 注入条目（allow_inject + 可选条件注入：仅当上下文出现管理员才注入）
        #    位置：紧随头部系统提示词之后（injection_insert_index 计算插入点）
        injection_item: dict[str, Any] | None = None
        if allow_inject:
            if not bool(self.config.inject.require_admin_in_context) or hit:
                injection_item = build_injection_item(
                    self._render_prompt(),
                    role=str(self.config.inject.inject_role or ROLE_SYSTEM),
                )

        if annotated <= 0 and cleaned <= 0 and injection_item is None:
            return None

        modified = dict(kwargs)
        if annotated > 0 or cleaned > 0:
            modified["items"] = processed_items
        if injection_item is not None:
            base = modified.get("items", processed_items)
            new_items = list(base)
            new_items.insert(injection_insert_index(base), injection_item)
            modified["items"] = new_items
        return modified

    # ==================== Hook：标注 + 注入（Planner） ====================

    @HookHandler(
        "maisaka.planner.before_request",
        name="admin_identity_planner",
        description="Planner 请求前：给管理员消息追加 (QQ号) 标注并注入管理员提示词",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
        timeout_ms=0,
    )
    async def hook_planner(self, **kwargs: Any) -> dict[str, Any]:
        """Planner 请求前主入口：标注管理员消息 + 注入管理员提示词。"""
        try:
            modified = self._apply_all(
                kwargs,
                allow_annotate=self._annotate_active(),
                allow_inject=bool(self.config.plugin.enabled)
                and bool(self.config.inject.inject_into_planner),
            )
            if modified is None:
                return {"action": "continue"}
            self.ctx.logger.debug("管理员标注/注入已应用到 Planner 请求")
            return {"action": "continue", "modified_kwargs": modified}
        except Exception as e:
            self.ctx.logger.warning("管理员标注/注入异常（本次不处理，继续原请求）：%s", e)
            return {"action": "continue"}

    # ==================== Hook：注入（Replyer，可选） ====================

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="admin_identity_replyer",
        description="回复器请求前：给管理员消息追加 (QQ号) 标注并注入管理员提示词（可选，整体开关 inject_into_replyer）",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        error_policy=ErrorPolicy.SKIP,
        timeout_ms=0,
    )
    async def hook_replyer(self, **kwargs: Any) -> dict[str, Any]:
        """回复器路径（整体默认关）：标注 + 注入。

        回复器上下文同样由带 planner 前缀的历史消息构成（SessionBackedMessage），
        因此按 QQ 号标注与反伪造清洗在这里同样成立。仅当 inject_into_replyer 开启时
        本 Hook 才做任何事，关闭时放空——保持与「仅 planner 生效」的默认行为一致。
        """
        try:
            if not bool(self.config.inject.inject_into_replyer):
                return {"action": "continue"}
            modified = self._apply_all(
                kwargs,
                allow_annotate=self._annotate_active(),
                allow_inject=True,
            )
            if modified is None:
                return {"action": "continue"}
            self.ctx.logger.debug("管理员标注/注入已应用到 Replyer 请求")
            return {"action": "continue", "modified_kwargs": modified}
        except Exception as e:
            self.ctx.logger.warning("回复器管理员标注/注入异常（本次不处理，继续原请求）：%s", e)
            return {"action": "continue"}

    # ==================== Hook：发送者缓存 ====================

    @HookHandler(
        "chat.receive.before_process",
        name="admin_identity_receive_gate",
        description="入站消息预处理前记录「消息 ID → 发送者」与「会话 ID → 是否群聊」，供请求前识别管理员与判定生效范围",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,
        timeout_ms=0,
    )
    async def hook_receive_gate(self, **kwargs: Any) -> dict[str, Any]:
        """记录放行消息的「消息 ID → 发送者」与「会话 ID → 是否群聊」；不做任何拦截。"""
        try:
            message = kwargs.get("message")
            # 会话类型是纯缓存信息（无副作用），即便插件关闭也照记，便于开启后立即生效
            session_id, is_group = extract_session_info_from_message(message)
            if session_id and is_group is not None:
                self._session_kinds.record(session_id, is_group)
            if not bool(self.config.plugin.enabled):
                return {"action": "continue"}
            user_id = extract_user_id_from_message(message)
            message_id = message.get("message_id") if isinstance(message, dict) else None
            if user_id and message_id:
                self._sender_cache.record(message_id, user_id)
            return {"action": "continue"}
        except Exception as e:
            self.ctx.logger.warning("发送者/会话类型缓存记录异常（放行本条）：%s", e)
            return {"action": "continue"}

    # ==================== 命令 ====================

    @Command(
        "admin_identity_status",
        description="查看管理员身份标注插件状态：名单、标注与注入开关",
        pattern=r"(?<!\S)/?(?:管理员标注|admin_identity)\s*$",
    )
    async def cmd_status(self, **kwargs: Any) -> tuple[bool, str, bool]:
        """输出当前状态（纯文本回复，仅声明 send.text 能力）。"""
        stream_id = str(kwargs.get("stream_id") or "")
        lines = self._describe_state(session_id=stream_id)
        try:
            await self.ctx.send.text("\n".join(lines), stream_id)
        except Exception as e:
            self.ctx.logger.warning("发送管理员标注状态失败：%s", e)
        return True, "已发送管理员标注状态", True

    def _describe_state(self, *, session_id: str = "") -> List[str]:
        """生成当前状态描述文本（日志 / 命令共用）。

        ``session_id`` 非空时额外输出当前会话类型与是否生效（命令内诊断用）。
        """
        admins = self._admins()
        scope_mode = str(self.config.scope.mode or SCOPE_ALL)
        lines = ["【管理员身份标注】当前状态"]
        lines.append(
            "生效范围：{}".format(
                "仅群聊（私聊不标注、不清洗、不注入）"
                if scope_mode == SCOPE_GROUP_ONLY
                else "私聊 + 群聊（全部会话）"
            )
        )
        if session_id:
            is_group = self._session_kinds.is_group(session_id)
            chat_type = "群聊" if is_group else "私聊" if is_group is False else "未知（该会话尚无入站消息记录）"
            lines.append(
                "当前会话：{} → {}".format(
                    chat_type,
                    "生效" if self._scope_active(session_id) else "不生效",
                )
            )
        lines.append(
            "管理员名单（{}）：{}".format(
                len(admins),
                "、".join(admin.display() for admin in admins) if admins else "（空）",
            )
        )
        lines.append(
            "QQ 号标注：{}（仅对 msg_id 反查命中的真实管理员 QQ 生效）".format(
                "开" if self._annotate_active() else "关",
            )
        )
        lines.append(
            "反伪造清洗：开（非管理员消息若携带管理员 QQ 标注会被剥除，始终生效）"
        )
        lines.append(
            "提示词注入：Planner{} / 回复器{}（角色 {}）".format(
                "✓" if bool(self.config.inject.inject_into_planner) else "✗",
                "✓" if bool(self.config.inject.inject_into_replyer) else "✗",
                self.config.inject.inject_role,
            )
        )
        lines.append(
            "条件注入：{}".format(
                "仅当上下文出现管理员时注入"
                if bool(self.config.inject.require_admin_in_context)
                else "关闭（每次都注入）"
            )
        )
        return lines

    # ==================== 配置版本兼容 ====================

    def _check_config_version(self) -> None:
        """检测配置版本并提示兼容（缺失字段由 Runner 按默认值自动补齐）。"""
        try:
            raw = self.get_plugin_config_data()
            current = str((raw.get("plugin") or {}).get("config_version") or "").strip()
        except Exception:
            return
        if current and current != SUPPORTED_CONFIG_VERSION:
            self.ctx.logger.info(
                "检测到旧版配置（config_version=%s，当前支持 %s），缺失字段已按默认值自动补齐",
                current,
                SUPPORTED_CONFIG_VERSION,
            )

    def _validate_config(self) -> None:
        """校验配置并记录警告（不影响加载）。"""
        if not str(self.config.inject.prompt_template or "").strip():
            self.ctx.logger.warning("提示词模板为空：名单再非空也不会注入任何内容")
        if self.config.inject.prompt_template and "{admin_list}" not in str(
            self.config.inject.prompt_template
        ):
            self.ctx.logger.info("提示词模板中不含 {admin_list} 占位符：名单不会出现在提示词里")

    # ==================== 生命周期 ====================

    async def on_load(self) -> None:
        self._check_config_version()
        self._validate_config()
        admins = self._admins()
        if not admins:
            self.ctx.logger.info(
                "管理员身份标注已加载：管理员名单为空，暂不标注、不注入（请在配置中填写名单）"
            )
            return
        self.ctx.logger.info("管理员身份标注已加载：\n%s", "\n".join(self._describe_state()))

    async def on_unload(self) -> None:
        self._sender_cache.clear()
        self._session_kinds.clear()
        self.ctx.logger.info("管理员身份标注已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        del config_data
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        self._check_config_version()
        self._validate_config()
        self.ctx.logger.info(
            "管理员身份标注配置已热更新（version=%s）：\n%s",
            version,
            "\n".join(self._describe_state()),
        )


def create_plugin() -> CateyeAdminIdentityPlugin:
    """Runner 加载入口。"""
    return CateyeAdminIdentityPlugin()
