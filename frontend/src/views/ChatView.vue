<template>
	<div
		ref="rootEl"
		class="jv-root"
		:class="{ 'jv-dark': effectiveDark }"
		:style="paletteVars"
		style="
			--rad: 8px;
			font-family: 'Inter', system-ui, sans-serif;
			height: 100%;
			width: 100%;
			display: flex;
			color: var(--text);
			background: var(--surface);
			overflow: hidden;
			position: relative;
		"
	>
		<!-- ============ MAIN ============ -->
		<main
			style="
				flex: 1;
				display: flex;
				flex-direction: column;
				height: 100%;
				min-width: 0;
				background: var(--surface);
			"
		>
			<header
				style="
					height: 52px;
					flex: none;
					border-bottom: 1px solid var(--border);
					display: flex;
					align-items: center;
					padding: 0 18px;
					gap: 12px;
				"
			>
				<!-- (no expand button here — the collapsed rail's top button already expands;
				     two visible "Expand sidebar" controls was confusing) -->
				<div
					style="display: flex; flex-direction: column; line-height: 1.15; min-width: 0"
				>
					<span
						style="
							font-size: 14px;
							font-weight: 600;
							letter-spacing: -0.01em;
							white-space: nowrap;
							overflow: hidden;
							text-overflow: ellipsis;
						"
						>{{ currentTitle }}</span
					>
					<span style="font-size: 11.5px; color: var(--text-3)">{{ headerSub }}</span>
				</div>
				<div style="margin-left: auto; display: flex; align-items: center; gap: 8px">
					<!-- "Go to Desk" — at the rightmost end of the cluster (chat only, via CSS order)
					     (uniform with LayoutHeader across every page) -->
					<button
						class="jv-iconbtn"
						@click="openErpDesk"
						title="Open ERPNext Desk"
						aria-label="Open ERPNext Desk"
						style="
							order: 99;
							width: 32px;
							height: 32px;
							display: flex;
							align-items: center;
							justify-content: center;
							background: transparent;
							border: 1px solid var(--border);
							border-radius: 7px;
							cursor: pointer;
						"
					>
						<svg
							width="16"
							height="16"
							viewBox="0 0 24 24"
							fill="none"
							stroke="var(--text-2)"
							stroke-width="1.7"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
							<path d="M15 3h6v6M10 14 21 3" />
						</svg>
					</button>
					<!-- Save this conversation's prompts as a reusable macro (opens the /macros editor pre-filled) -->
					<button
						v-if="canSaveAsMacro"
						class="jv-modelpill"
						@click="saveConversationAsMacro"
						title="Save this chat's prompts as a macro"
						style="
							display: flex;
							align-items: center;
							gap: 6px;
							padding: 5px 10px;
							background: var(--surface-1);
							border: 1px solid var(--border);
							border-radius: 20px;
							cursor: pointer;
							font-family: inherit;
						"
					>
						<svg
							width="13"
							height="13"
							viewBox="0 0 24 24"
							fill="none"
							stroke="var(--text-2)"
							stroke-width="1.7"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M5 3l14 9-14 9V3z" />
						</svg>
						<span style="font-size: 12px; color: var(--text-2); font-weight: 500"
							>Save as macro</span
						>
					</button>
					<!-- Model picker: switch the LLM model for this conversation -->
					<div class="jv-modelmenu-wrap" style="position: relative">
						<button
							class="jv-modelpill"
							@click="modelMenuOpen = !modelMenuOpen"
							title="Model and effort"
							style="
								display: flex;
								align-items: center;
								gap: 7px;
								padding: 5px 10px;
								background: var(--surface-1);
								border: 1px solid var(--border);
								border-radius: 20px;
								cursor: pointer;
								font-family: inherit;
							"
						>
							<svg
								width="13"
								height="13"
								viewBox="0 0 24 24"
								fill="none"
								stroke="var(--text-2)"
								stroke-width="1.7"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<ellipse cx="12" cy="5" rx="9" ry="3" />
								<path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5" />
								<path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3" />
							</svg>
							<span
								style="font-size: 12px; color: var(--text-2); font-weight: 500"
								>{{ modelLabel }}</span
							>
							<span
								v-if="thinkingOverride"
								style="font-size: 11px; color: var(--text-3); font-weight: 450"
								>· {{ thinkingOverride }}</span
							>
							<svg
								width="12"
								height="12"
								viewBox="0 0 24 24"
								fill="none"
								stroke="var(--text-3)"
								stroke-width="1.9"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path d="m6 9 6 6 6-6" />
							</svg>
						</button>
						<div
							v-if="modelMenuOpen"
							style="
								position: absolute;
								top: calc(100% + 6px);
								right: 0;
								min-width: 224px;
								background: var(--surface);
								border: 1px solid var(--border-2);
								border-radius: 10px;
								box-shadow: 0 8px 24px rgba(20, 20, 30, 0.14);
								padding: 5px;
								z-index: 30;
							"
						>
							<!-- Effort first: it is the control that always applies, even when the
								     customer has exactly one configured model. Levels come from the server
								     (thinking_levels) because Jarvis Conversation.thinking_override is a
								     Select - offering a level it rejects would fail the save. -->
							<div
								style="
									padding: 3px 9px 6px;
									font-size: 10px;
									color: var(--text-3);
									font-weight: 600;
									text-transform: uppercase;
									letter-spacing: 0.03em;
								"
							>
								Effort
							</div>
							<button
								class="jv-menuitem"
								:class="{ on: !thinkingOverride }"
								@click="selectThinking('')"
							>
								<svg
									width="14"
									height="14"
									viewBox="0 0 24 24"
									fill="none"
									stroke="currentColor"
									stroke-width="1.8"
									stroke-linecap="round"
									stroke-linejoin="round"
									style="flex: none"
								>
									<path
										d="M12 2v4M12 18v4M4.9 4.9l2.8 2.8M16.3 16.3l2.8 2.8M2 12h4M18 12h4M4.9 19.1l2.8-2.8M16.3 7.7l2.8-2.8"
									/>
								</svg>
								<span style="flex: 1">Auto</span>
								<svg
									v-if="!thinkingOverride"
									width="14"
									height="14"
									viewBox="0 0 24 24"
									fill="none"
									stroke="var(--text)"
									stroke-width="2.2"
									stroke-linecap="round"
									stroke-linejoin="round"
									style="flex: none"
								>
									<path d="M20 6 9 17l-5-5" />
								</svg>
							</button>
							<button
								v-for="lvl in thinkingLevels"
								:key="lvl"
								class="jv-menuitem"
								:class="{ on: lvl === thinkingOverride }"
								@click="selectThinking(lvl)"
							>
								<span style="flex: 1; text-transform: capitalize">{{ lvl }}</span>
								<svg
									v-if="lvl === thinkingOverride"
									width="14"
									height="14"
									viewBox="0 0 24 24"
									fill="none"
									stroke="var(--text)"
									stroke-width="2.2"
									stroke-linecap="round"
									stroke-linejoin="round"
									style="flex: none"
								>
									<path d="M20 6 9 17l-5-5" />
								</svg>
							</button>
							<template v-if="pickableModels.length">
								<!-- Auto: let Jarvis pick, divided from the explicit models. Clearing the pin
								     is openclaw's "/model default": the session stops overriding the configured
								     primary and inherits it again. -->
								<button
									class="jv-menuitem"
									:class="{ on: !modelOverride }"
									@click="selectModel('')"
								>
									<svg
										width="14"
										height="14"
										viewBox="0 0 24 24"
										fill="none"
										stroke="currentColor"
										stroke-width="1.8"
										stroke-linecap="round"
										stroke-linejoin="round"
										style="flex: none"
									>
										<path
											d="M12 2v4M12 18v4M4.9 4.9l2.8 2.8M16.3 16.3l2.8 2.8M2 12h4M18 12h4M4.9 19.1l2.8-2.8M16.3 7.7l2.8-2.8"
										/>
									</svg>
									<span style="flex: 1"
										>Auto
										<span style="color: var(--text-3); font-weight: 450"
											>· {{ ui.llm_model || "default" }}</span
										></span
									>
									<svg
										v-if="!modelOverride"
										width="14"
										height="14"
										viewBox="0 0 24 24"
										fill="none"
										stroke="var(--text)"
										stroke-width="2.2"
										stroke-linecap="round"
										stroke-linejoin="round"
										style="flex: none"
									>
										<path d="M20 6 9 17l-5-5" />
									</svg>
								</button>
								<!-- One group per provider, but only labelled by provider when the customer
								     actually has more than one. A subscription pool stores provider="" on every
								     row, so a single flat "Model" header is the honest rendering there. -->
								<template v-for="g in modelsByProvider" :key="g.provider">
									<div
										style="
											height: 1px;
											background: var(--border);
											margin: 5px 2px;
										"
									></div>
									<div
										style="
											padding: 3px 9px 6px;
											font-size: 10px;
											color: var(--text-3);
											font-weight: 600;
											text-transform: uppercase;
											letter-spacing: 0.03em;
										"
									>
										{{ showProviders ? g.provider : "Model" }}
									</div>
									<button
										v-for="r in g.models"
										:key="g.provider + '/' + r.model"
										class="jv-menuitem"
										:class="{ on: r.model === modelOverride }"
										@click="selectModel(r.model)"
									>
										<span style="flex: 1">{{ r.model }}</span>
										<span
											v-if="r.tier"
											style="
												font-size: 10px;
												color: var(--text-3);
												margin-right: 6px;
											"
											>{{ r.tier }}</span
										>
										<svg
											v-if="r.model === modelOverride"
											width="14"
											height="14"
											viewBox="0 0 24 24"
											fill="none"
											stroke="var(--text)"
											stroke-width="2.2"
											stroke-linecap="round"
											stroke-linejoin="round"
											style="flex: none"
										>
											<path d="M20 6 9 17l-5-5" />
										</svg>
									</button>
								</template>
							</template>
						</div>
					</div>
					<!-- Connect phone: shows a QR the mobile app scans to onboard -->
					<button
						class="jv-iconbtn"
						@click="showConnect = true"
						title="Connect phone"
						aria-label="Connect phone"
						style="
							width: 32px;
							height: 32px;
							display: flex;
							align-items: center;
							justify-content: center;
							background: transparent;
							border: 1px solid var(--border);
							border-radius: 7px;
							cursor: pointer;
						"
					>
						<svg
							width="16"
							height="16"
							viewBox="0 0 24 24"
							fill="none"
							stroke="var(--text-2)"
							stroke-width="1.7"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<rect x="7" y="2" width="10" height="20" rx="2" />
							<path d="M11 18h2" />
						</svg>
					</button>
					<button
						class="jv-iconbtn"
						@click="toggleTheme"
						title="Change theme (light / dark / system)"
						style="
							width: 32px;
							height: 32px;
							display: flex;
							align-items: center;
							justify-content: center;
							background: transparent;
							border: 1px solid var(--border);
							border-radius: 7px;
							cursor: pointer;
						"
					>
						<svg
							v-if="effectiveDark"
							width="16"
							height="16"
							viewBox="0 0 24 24"
							fill="none"
							stroke="var(--text-2)"
							stroke-width="1.7"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<circle cx="12" cy="12" r="4" />
							<path
								d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"
							/>
						</svg>
						<svg
							v-else
							width="16"
							height="16"
							viewBox="0 0 24 24"
							fill="none"
							stroke="var(--text-2)"
							stroke-width="1.7"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
						</svg>
					</button>
				</div>
			</header>

			<!-- recurring every-third-new-chat business-note banner. Lives at the top of
			     the chat area (booting/welcome/thread all render below it) so it never
			     crowds the composer or the send button. "Maybe later" just hides the card
			     client-side for this chat - the cadence itself is the snooze, so it comes
			     back on the next multiple-of-three chat with no follow-up question. "Don't
			     ask again" is the durable, permanent, server-side dismiss. -->
			<div v-if="bizGreeting.show" class="jv-greeting-banner">
				<div class="jv-nudge" style="margin: 0">
					<div class="jv-nudge-head">
						<div class="jv-nudge-q">
							<b>Tell me how your business runs</b> and I will use it in every
							answer.
						</div>
						<button
							class="jv-nudge-x"
							title="Maybe later"
							aria-label="Maybe later"
							@click="greetingLater"
						>
							<svg
								width="13"
								height="13"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="2"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path d="M18 6 6 18M6 6l12 12" />
							</svg>
						</button>
					</div>
					<div class="jv-nudge-actions">
						<button
							class="jv-btn jv-btn--primary"
							style="height: 30px; padding: 0 12px"
							@click="greetingShowMeWhere"
						>
							Add business notes<svg
								width="13"
								height="13"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="2"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path d="M5 12h14" />
								<path d="m13 6 6 6-6 6" />
							</svg>
						</button>
						<button class="jv-nudge-type" @click="greetingLater">Maybe later</button>
						<button class="jv-nudge-type" @click="greetingNeverAsk">
							Don't ask again
						</button>
					</div>
				</div>
			</div>

			<!-- initial load: a quiet spinner so the welcome screen doesn't flash
			     before the open conversation finishes loading on refresh -->
			<div
				v-if="booting"
				style="flex: 1; display: flex; align-items: center; justify-content: center"
			>
				<svg
					class="jv-spin"
					width="22"
					height="22"
					viewBox="0 0 24 24"
					fill="none"
					stroke="var(--text-3)"
					stroke-width="2.4"
					stroke-linecap="round"
				>
					<path d="M12 3a9 9 0 1 0 9 9" />
				</svg>
			</div>
			<!-- ===== WELCOME ===== -->
			<div
				v-else-if="showWelcome"
				style="
					flex: 1;
					overflow-y: auto;
					display: flex;
					flex-direction: column;
					align-items: center;
					justify-content: center;
					padding: 32px;
				"
			>
				<div style="width: 100%; max-width: 680px; text-align: center">
					<!-- The brand mark, from its single source of truth. This was a
					     hand-pasted copy whose gradient read `var(--cta)` as its first
					     stop; when #294 repointed --cta from indigo to near-black, this
					     mark silently became near-black->purple while the sidebar mark
					     (UserMenu) stayed blue->purple — two different logos on one
					     screen. The purple glow shadow is dropped (design.md §5 #4). -->
					<JarvisMark :size="54" :radius="14" style="margin: 0 auto 18px" />
					<h1
						class="jv-welcome-h1"
						style="
							font-size: 30px;
							font-weight: 640;
							letter-spacing: -0.03em;
							margin: 0 0 8px;
							overflow-wrap: anywhere;
						"
					>
						{{ greeting }}, {{ firstName }}
					</h1>
					<p
						style="
							font-size: 14.5px;
							color: var(--text-2);
							margin: 0 0 26px;
							line-height: 1.5;
						"
					>
						Ask about your ERP data, run a workflow, or draft something.
						{{ agentName }}
						is connected to your
						<strong style="color: var(--text); font-weight: 600">ERPNext</strong>
						instance.
					</p>
					<div
						class="jv-welcome-grid"
						style="
							display: grid;
							grid-template-columns: 1fr 1fr;
							gap: 11px;
							text-align: left;
						"
					>
						<button
							v-for="s in suggestions"
							:key="s.title"
							type="button"
							class="jv-suggest"
							@click="fillInput(s.prompt)"
							style="
								display: flex;
								gap: 11px;
								padding: 14px;
								background: var(--surface);
								border: 1px solid var(--border);
								border-radius: 10px;
								cursor: pointer;
								transition: border-color 0.12s, background 0.12s;
								text-align: left;
								font-family: inherit;
								color: inherit;
								width: 100%;
							"
						>
							<div
								:style="{
									width: '30px',
									height: '30px',
									flex: 'none',
									borderRadius: '8px',
									background: s.bg,
									color: s.fg,
									display: 'flex',
									alignItems: 'center',
									justifyContent: 'center',
								}"
								v-html="s.icon"
							></div>
							<div>
								<div
									style="font-size: 13.5px; font-weight: 550; margin-bottom: 2px"
								>
									{{ s.title }}
								</div>
								<div
									style="
										font-size: 12.5px;
										color: var(--text-3);
										line-height: 1.4;
									"
								>
									{{ s.prompt }}
								</div>
							</div>
						</button>
					</div>
				</div>
			</div>

			<!-- ===== CONVERSATION ===== -->
			<div
				v-else
				ref="threadEl"
				@scroll.passive="onThreadScroll"
				role="log"
				:aria-label="`Conversation with ${agentName}`"
				style="flex: 1; overflow-y: auto"
			>
				<div
					ref="threadInnerEl"
					class="jv-thread-inner"
					style="
						max-width: 1280px;
						margin: 0 auto;
						padding: 26px 40px 36px;
						display: flex;
						flex-direction: column;
						gap: 26px;
					"
				>
					<!-- macro run progress banner -->
					<div
						v-if="macroRun && macroRun.conversation === currentId"
						class="jv-macrobar"
						:class="{
							ok: macroRun.status === 'completed',
							err: macroRun.status === 'failed',
							stopped: macroRun.status === 'stopped',
						}"
					>
						<template v-if="macroRun.status === 'running'">
							<span class="jv-macrobar-dot spin"></span>
							<span class="jv-macrobar-txt"
								>Running macro — step {{ macroRun.step }}/{{ macroRun.total
								}}<template v-if="macroRun.label"
									>: {{ macroRun.label }}</template
								></span
							>
							<button class="jv-macrobar-stop" @click="stopMacro">Stop</button>
						</template>
						<template v-else-if="macroRun.status === 'completed'"
							><span class="jv-macrobar-chip">✓ Macro completed</span></template
						>
						<template v-else-if="macroRun.status === 'failed'"
							><span class="jv-macrobar-chip">✗ Macro failed</span></template
						>
						<template v-else-if="macroRun.status === 'stopped'"
							><span class="jv-macrobar-chip">⏹ Macro stopped</span></template
						>
					</div>
					<template v-for="(m, mi) in visibleMessages" :key="m.name">
						<div v-if="dayDividers[mi]" class="jv-daydivider">
							<span>{{ dayDividers[mi] }}</span>
						</div>
						<!-- an orphaned tool FAILURE (no assistant turn to hang it off) —
						     e.g. a delegate refused on its very first call. Rendered here
						     because the Activity accordion cannot hold it. -->
						<div
							v-if="m.role === 'tool' && orphanToolFailures[m.name]"
							class="jv-toolfail"
							role="alert"
						>
							<svg
								class="jv-toolfail-ico"
								width="15"
								height="15"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="2"
								stroke-linecap="round"
								stroke-linejoin="round"
								aria-hidden="true"
							>
								<circle cx="12" cy="12" r="9" />
								<path d="M12 7.5v5.5M12 16.2v.2" />
							</svg>
							<div class="jv-toolfail-main">
								<div class="jv-toolfail-title">
									{{ toolLabel(m.tool_name) }} didn't run
								</div>
								<div class="jv-toolfail-msg">
									{{ orphanToolFailures[m.name].message }}
								</div>
								<div
									v-if="orphanToolFailures[m.name].hint"
									class="jv-toolfail-hint"
								>
									{{ orphanToolFailures[m.name].hint }}
								</div>
							</div>
						</div>
						<!-- receipt chip: a confirmed / discarded / failed gated write, shown
						     inline in place of the confirmation card that used to vanish -->
						<ReceiptChip v-else-if="m.role === 'tool'" :message="m" />
						<!-- user -->
						<Message
							v-else-if="m.role === 'user'"
							variant="bubble"
							:text="m.content"
							:attachments="m.canvas"
							:timestamp="msgTime(m)"
							:timestampFull="msgTimeFull(m)"
							editable
							:copied="copiedId === m.name"
							:failed="m.failed"
							@edit="editCommand(m)"
							@copy="copyMsg(m.name, m.content)"
							@retry="resendFailed(m)"
							@open-attachment="openArtifact(m, $event)"
						/>
						<!-- assistant -->
						<!-- copied/@copy here drive Message's BUILT-IN trailer, which #below-body
						     suppresses — chat's real Copy button is in the slotted metabar below.
						     Kept so both call sites stay interface-identical. -->
						<Message
							v-else
							variant="row"
							:html="m.error ? '' : render(m.content)"
							:attachments="m.canvas"
							:timestamp="msgTime(m)"
							:timestampFull="msgTimeFull(m)"
							:copied="copiedId === m.name"
							@copy="copyMsg(m.name, stripBlocks(m.content))"
							@open-attachment="openArtifact(m, $event)"
						>
							<template #avatar>
								<JarvisMark :size="28" :radius="7" style="margin-top: 2px" />
							</template>
							<template #above-body>
								<!-- Activity: the tool calls (with input + output) that produced
								     this answer — openclaw-style, collapsible. -->
								<div
									v-if="
										showActivityDetail &&
										(activityByAssistant[m.name] || []).length
									"
									class="jv-activity"
								>
									<button
										class="jv-activity-head"
										@click="toggleActivity(m.name)"
										:aria-expanded="!!isActivityOpen(m.name)"
									>
										<svg
											class="jv-activity-chev"
											:class="{ open: isActivityOpen(m.name) }"
											width="12"
											height="12"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="2.2"
											stroke-linecap="round"
											stroke-linejoin="round"
										>
											<path d="M9 18l6-6-6-6" />
										</svg>
										<svg
											width="13"
											height="13"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="1.8"
											stroke-linecap="round"
											stroke-linejoin="round"
										>
											<path
												d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 1 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"
											/>
										</svg>
										<span class="jv-activity-count"
											>{{ (activityByAssistant[m.name] || []).length }} tool
											call{{
												(activityByAssistant[m.name] || []).length === 1
													? ""
													: "s"
											}}</span
										>
										<span
											v-if="!isActivityOpen(m.name)"
											class="jv-activity-preview"
											>{{ activityNames(m.name) }}</span
										>
									</button>
									<div v-if="isActivityOpen(m.name)" class="jv-activity-body">
										<div
											v-for="t in activityByAssistant[m.name] || []"
											:key="t.name"
											class="jv-tool"
											:class="{ open: toolOpen[t.name] }"
										>
											<button
												class="jv-tool-head"
												@click="toggleTool(t.name)"
											>
												<span
													class="jv-tool-dot"
													:class="
														t.tool_status === 'completed'
															? 'ok'
															: t.tool_status === 'running'
															? 'run'
															: 'err'
													"
												></span>
												<span class="jv-tool-name">{{
													toolLabel(t.tool_name)
												}}</span>
												<span class="jv-tool-status">{{
													t.tool_status
												}}</span>
												<svg
													class="jv-tool-chev"
													:class="{ open: toolOpen[t.name] }"
													width="11"
													height="11"
													viewBox="0 0 24 24"
													fill="none"
													stroke="currentColor"
													stroke-width="2.2"
													stroke-linecap="round"
													stroke-linejoin="round"
												>
													<path d="M9 18l6-6-6-6" />
												</svg>
											</button>
											<div v-if="toolOpen[t.name]" class="jv-tool-detail">
												<template v-if="prettyJson(t.tool_args)">
													<div class="jv-tool-io-k">Input</div>
													<pre class="jv-tool-io">{{
														prettyJson(t.tool_args)
													}}</pre>
												</template>
												<template v-if="prettyJson(t.tool_result)">
													<div class="jv-tool-io-k">Output</div>
													<pre class="jv-tool-io">{{
														prettyJson(t.tool_result)
													}}</pre>
												</template>
											</div>
										</div>
									</div>
								</div>
								<!-- Phase-0 admission (SUXI-4): a cancelled/aged-out queued turn
								     leaves a durable marker. Render it as a MUTED note, not the
								     red error alert - the user (or the system) cancelled it; it
								     is not a failure. -->
								<div
									v-if="m.error && errorInfo(m).code === 'cancelled'"
									role="status"
									style="
										display: flex;
										align-items: center;
										gap: 8px;
										font-size: 12.5px;
										color: var(--text-3);
										padding: 2px 0;
									"
								>
									<svg
										width="14"
										height="14"
										viewBox="0 0 24 24"
										fill="none"
										stroke="currentColor"
										stroke-width="2"
										stroke-linecap="round"
										stroke-linejoin="round"
									>
										<circle cx="12" cy="12" r="9" />
										<path d="M15 9l-6 6M9 9l6 6" />
									</svg>
									<span>{{ m.error }}</span>
								</div>
								<div
									v-else-if="m.error"
									role="alert"
									style="
										border: 1px solid var(--red-bd);
										border-radius: 11px;
										background: var(--red-bg);
										padding: 13px 15px;
										display: flex;
										align-items: flex-start;
										gap: 10px;
									"
								>
									<svg
										width="17"
										height="17"
										style="margin-top: 1px; flex: none"
										viewBox="0 0 24 24"
										fill="none"
										stroke="var(--red)"
										stroke-width="1.9"
										stroke-linecap="round"
										stroke-linejoin="round"
									>
										<path
											d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"
										/>
										<path d="M12 9v4M12 17h.01" />
									</svg>
									<div style="flex: 1">
										<div
											style="
												font-size: 13.5px;
												font-weight: 600;
												color: var(--red);
											"
										>
											{{ errorInfo(m).headline }}
										</div>
										<div
											v-if="errorInfo(m).noChange"
											style="
												font-size: 12px;
												color: var(--text-2);
												margin-top: 3px;
											"
										>
											No changes were made to your data.
										</div>
										<details style="margin-top: 4px">
											<summary
												style="
													font-size: 11.5px;
													color: var(--text-3);
													cursor: pointer;
												"
											>
												Show details
											</summary>
											<div
												style="
													font-size: 12px;
													color: var(--text-2);
													margin-top: 4px;
													line-height: 1.5;
													white-space: pre-wrap;
													overflow-wrap: anywhere;
												"
											>
												{{ m.error }}
											</div>
										</details>
										<button
											class="jv-retry"
											@click="retry(m.name)"
											:disabled="retrying"
											:style="{
												marginTop: '10px',
												display: 'inline-flex',
												alignItems: 'center',
												gap: '6px',
												padding: '6px 12px',
												background: 'var(--red)',
												color: '#fff',
												border: 'none',
												borderRadius: '7px',
												fontFamily: 'inherit',
												fontSize: '12px',
												fontWeight: '550',
												cursor: retrying ? 'default' : 'pointer',
												opacity: retrying ? 0.6 : 1,
											}"
										>
											{{ retrying ? "Retrying…" : "Retry" }}
										</button>
									</div>
								</div>
							</template>
							<template #below-body>
								<!-- The user stopped this reply. A SIBLING of the body, not part of
								     it: gated only on `stopped`, so it shows for a partial stop
								     (content present) and an empty one alike, and the renderer can
								     never mangle it. Muted, never an error tone - a deliberate stop
								     is not a failure. -->
								<div v-if="m.stopped" class="jv-stopped">
									You stopped this reply.
								</div>
								<!-- rich action card the agent emits (doc confirm / email draft) -->
								<template v-if="actionFor === m.name && activeAction">
									<!-- email draft -->
									<div v-if="activeAction.kind === 'email'" class="jv-email">
										<div class="jv-email-head">
											<div class="jv-email-line">
												<span class="jv-email-k">To</span
												><span class="jv-email-v">{{
													activeAction.to
												}}</span>
											</div>
											<div class="jv-email-line">
												<span class="jv-email-k">Subject</span
												><span class="jv-email-v jv-email-subj">{{
													activeAction.subject
												}}</span>
											</div>
										</div>
										<div class="jv-email-body">{{ activeAction.body }}</div>
										<div class="jv-action-foot">
											<!-- send_email is a gated write (issue #186): the actual Send confirmation
											     arrives as an action:pending card, not a model-visible approval message.
											     This draft card stays a read-only preview (copy / regenerate). -->
											<button
												class="jv-action-2nd"
												@click="copyText(activeAction.body)"
											>
												<svg
													width="14"
													height="14"
													viewBox="0 0 24 24"
													fill="none"
													stroke="currentColor"
													stroke-width="1.8"
													stroke-linecap="round"
													stroke-linejoin="round"
												>
													<rect
														x="9"
														y="9"
														width="13"
														height="13"
														rx="2"
													/>
													<path
														d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"
													/></svg
												>Copy
											</button>
											<button
												class="jv-action-2nd"
												@click="
													actionSend('Regenerate that email, please.')
												"
											>
												<svg
													width="14"
													height="14"
													viewBox="0 0 24 24"
													fill="none"
													stroke="currentColor"
													stroke-width="1.8"
													stroke-linecap="round"
													stroke-linejoin="round"
												>
													<path
														d="M3 2v6h6M21 12a9 9 0 1 1-3-6.7L21 8"
													/></svg
												>Regenerate
											</button>
										</div>
										<!-- Rollout window (#13): a legacy container may still emit this email
										     card whose Send button was removed. The real Send confirmation
										     arrives as an action:pending card; note that here so the draft is
										     not a dead end while every container upgrades. -->
										<div class="jv-legacy-note">{{ LEGACY_GATE_NOTE }}</div>
									</div>
									<!-- create/update → read-only summary/diff card (Task 1.3); Edit opens the full panel -->
									<div
										v-else-if="isEditVerb(activeAction)"
										class="jv-action jv-summary"
									>
										<div class="jv-action-head">
											<svg
												width="15"
												height="15"
												viewBox="0 0 24 24"
												fill="none"
												stroke="var(--cta)"
												stroke-width="1.8"
												stroke-linecap="round"
												stroke-linejoin="round"
											>
												<path
													d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"
												/>
												<path d="M14 2v6h6" />
											</svg>
											<span class="jv-action-title">
												{{
													activeAction.verb === "update"
														? "Update"
														: "Create"
												}}
												{{ activeAction.doctype
												}}<template
													v-if="
														summaryState.model &&
														summaryState.model.docName
													"
												>
													· {{ summaryState.model.docName }}</template
												>
											</span>
										</div>

										<div v-if="summaryState.view" class="jv-summary-body">
											<div
												v-if="summaryState.view.headline"
												class="jv-summary-headline"
											>
												{{ summaryState.view.headline }}
											</div>
											<dl
												v-if="summaryState.view.kind === 'create'"
												class="jv-summary-fields"
											>
												<template
													v-for="(r, i) in summaryState.view.rows"
													:key="i"
												>
													<dt>{{ r.label }}</dt>
													<dd>{{ r.value }}</dd>
												</template>
											</dl>
											<div v-else class="jv-summary-diff">
												<div
													v-for="d in summaryState.view.diff"
													:key="d.label"
													class="jv-summary-diffrow"
												>
													<span class="jv-summary-difflbl">{{
														d.label
													}}</span>
													<span class="jv-summary-from">{{
														d.from || "(empty)"
													}}</span>
													<span class="jv-summary-arrow">-&gt;</span>
													<span class="jv-summary-to">{{
														d.to || "(empty)"
													}}</span>
												</div>
												<div
													v-if="!summaryState.view.diff.length"
													class="jv-summary-nochange"
												>
													No field changes.
												</div>
											</div>

											<div
												v-for="t in summaryState.view.tables"
												:key="t.fieldname"
												class="jv-summary-table"
											>
												<div class="jv-summary-table-h">
													{{ t.label }} ({{ t.count }})
												</div>
												<div class="jv-summary-gridwrap">
													<table class="jv-summary-grid">
														<thead
															v-if="t.columns && t.columns.length"
														>
															<tr>
																<th
																	v-for="(col, ci) in t.columns"
																	:key="ci"
																>
																	{{ col }}
																</th>
															</tr>
														</thead>
														<tbody>
															<tr
																v-for="(row, i) in t.rows"
																:key="i"
															>
																<td
																	v-for="(c, ci) in row.cells"
																	:key="ci"
																>
																	{{ c }}
																</td>
															</tr>
														</tbody>
													</table>
												</div>
											</div>
										</div>
										<!-- A draft we could not build is a FAILURE, not a pending state: style
										     it as one (it shared jv-summary-loading with "Preparing summary..."
										     and read as a placeholder), and drop the footer entirely - three
										     disabled buttons on a dead card are worse than no buttons, because
										     they look like something you should be able to press. -->
										<div
											v-else-if="summaryState.error"
											role="alert"
											class="jv-summary-body jv-summary-err"
										>
											<svg
												width="16"
												height="16"
												viewBox="0 0 24 24"
												fill="none"
												stroke="var(--red)"
												stroke-width="1.9"
												stroke-linecap="round"
												stroke-linejoin="round"
											>
												<path
													d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"
												/>
												<path d="M12 9v4M12 17h.01" />
											</svg>
											<span>{{ summaryState.error }}</span>
										</div>
										<div v-else class="jv-summary-body jv-summary-loading">
											Preparing summary...
										</div>

										<div
											v-if="summaryState.model && summaryState.model.error"
											style="margin: 0 14px 10px"
										>
											<ActionError :error="summaryState.model.error" />
										</div>

										<template v-if="!summaryState.error">
											<div class="jv-action-foot">
												<button
													class="jv-action-primary"
													:disabled="
														!summaryState.model ||
														(summaryState.model &&
															summaryState.model.applying) ||
														convStreaming
													"
													:title="
														convStreaming
															? 'Waiting for the current reply to finish'
															: ''
													"
													@click="confirmSummary"
												>
													{{
														summaryState.model &&
														summaryState.model.applying
															? "Saving..."
															: activeAction.verb === "update"
															? "Confirm update"
															: "Confirm create"
													}}
												</button>
												<button
													class="jv-action-2nd"
													:disabled="!summaryState.model"
													@click="previewSummary"
												>
													Preview
												</button>
												<button
													class="jv-action-2nd"
													:disabled="!summaryState.model"
													@click="
														openDraftPanel({
															verb: activeAction.verb || 'create',
															...activeAction,
														})
													"
												>
													Edit
												</button>
											</div>
											<div class="jv-summary-hint">
												Want a change? just tell me, e.g. "make Widget A
												qty 12".
											</div>
										</template>
									</div>
									<!-- submit/cancel/delete/amend are gated writes (issue #186): the real
									     confirmation is the action:pending card rendered below the thread,
									     which carries the server-minted token. A model-authored confirm card
									     for these verbs applies nothing. During the persona rollout window
									     (#12) a legacy container may still emit one; render a note in place of
									     the removed button instead of a dead card, and never wire it to the
									     removed applyAction path. -->
									<div v-else class="jv-legacy-note">{{ LEGACY_GATE_NOTE }}</div>
								</template>
								<!-- confirm / cancel (fallback, simple label) -->
								<div v-else-if="confirmFor === m.name" class="jv-confirm">
									<svg
										width="16"
										height="16"
										viewBox="0 0 24 24"
										fill="none"
										stroke="var(--amber)"
										stroke-width="1.8"
										stroke-linecap="round"
										stroke-linejoin="round"
									>
										<path
											d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"
										/>
										<path d="M12 9v4M12 17h.01" />
									</svg>
									<span class="jv-confirm-label">{{
										confirmLabel(m) || "Apply this change?"
									}}</span>
									<button
										class="jv-confirm-no"
										@click="answerConfirm(false, confirmLabel(m))"
									>
										Cancel
									</button>
									<button
										class="jv-confirm-yes"
										@click="answerConfirm(true, confirmLabel(m))"
									>
										Confirm
									</button>
								</div>
								<!-- interactive clarifying questions (Claude-style cards; one
								     Submit). Keyed by message: AskCard holds the draft picks and
								     must remount, not merely re-render, when a new ask arrives. -->
								<AskCard
									v-else-if="askFor === m.name && activeAsk"
									:key="m.name"
									:spec="activeAsk"
									@submit="send"
								/>
								<!-- record cards: scrollable card strip instead of a wide table -->
								<div v-if="cardsOf(m)" class="jv-cards">
									<div v-if="cardsOf(m).title" class="jv-cards-title">
										{{ cardsOf(m).title }}
									</div>
									<div class="jv-cards-strip">
										<div
											v-for="(c, ci) in pagedCards(m)"
											:key="cardPageOf(m) + '-' + ci"
											class="jv-card"
										>
											<a
												v-if="c.doctype && c.name"
												:href="`/app/${_deskSlug(
													c.doctype
												)}/${encodeURIComponent(c.name)}`"
												target="_blank"
												rel="noopener"
												class="jv-card-title jv-card-link"
												:title="'Open ' + c.doctype"
												>{{ c.title }}</a
											>
											<div v-else class="jv-card-title">{{ c.title }}</div>
											<div v-if="c.subtitle" class="jv-card-sub">
												{{ c.subtitle }}
											</div>
											<div
												v-for="(f, fi) in c.fields"
												:key="fi"
												class="jv-card-field"
											>
												<span class="jv-card-k">{{ f.label }}</span
												><span class="jv-card-v">{{ f.value }}</span>
											</div>
										</div>
									</div>
									<!-- long lists paginate — an endless horizontal scroll loses your place -->
									<div
										v-if="cardsOf(m).cards.length > CARD_PAGE_SIZE"
										class="jv-cards-pager"
									>
										<button
											class="jv-cards-pgbtn"
											:disabled="cardPageOf(m) === 0"
											@click="stepCardPage(m, -1)"
											aria-label="Previous cards"
										>
											‹
										</button>
										<span class="jv-cards-pginfo"
											>{{ cardPageOf(m) * CARD_PAGE_SIZE + 1 }}–{{
												Math.min(
													(cardPageOf(m) + 1) * CARD_PAGE_SIZE,
													cardsOf(m).cards.length
												)
											}}
											of {{ cardsOf(m).cards.length }}</span
										>
										<button
											class="jv-cards-pgbtn"
											:disabled="
												(cardPageOf(m) + 1) * CARD_PAGE_SIZE >=
												cardsOf(m).cards.length
											"
											@click="stepCardPage(m, 1)"
											aria-label="Next cards"
										>
											›
										</button>
									</div>
								</div>
								<!-- save-as-macro card: the agent proposed a reusable macro -->
								<div v-if="macroCardOf(m)" class="jv-macrocard">
									<div class="jv-macrocard-ic">
										<svg
											width="16"
											height="16"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="1.7"
											stroke-linecap="round"
											stroke-linejoin="round"
										>
											<path d="M5 3l14 9-14 9V3z" />
										</svg>
									</div>
									<div class="jv-macrocard-txt">
										<span class="jv-macrocard-title">{{
											macroCardOf(m).name || "Macro"
										}}</span>
										<span class="jv-macrocard-sub"
											>{{ macroCardOf(m).steps.length }} step{{
												macroCardOf(m).steps.length === 1 ? "" : "s"
											}}<template v-if="macroCardOf(m).description">
												· {{ macroCardOf(m).description }}</template
											></span
										>
									</div>
									<button
										class="jv-macrocard-btn"
										@click="openMacroFromCard(macroCardOf(m))"
									>
										Save as macro
									</button>
								</div>
								<!-- inline charts: ECharts rendered from the agent's jarvis-chart spec -->
								<div
									v-for="(spec, ci) in chartsOf(m)"
									:key="'chart' + ci"
									class="jv-chartwrap"
								>
									<JvChart :spec="spec" :dark="effectiveDark" />
								</div>
								<!-- rich outputs: agent artifacts rendered by type (sandboxed) -->
								<template v-for="cv in m.canvas || []" :key="cv.name">
									<!-- generated image → clickable thumbnail (click to enlarge) -->
									<button
										v-if="cv.type === 'image' && cv.file_url"
										class="jv-img-artifact"
										@click="openArtifact(m, cv)"
										:title="'Open ' + cv.title"
									>
										<img :src="cv.file_url" :alt="cv.title" loading="lazy" />
										<span class="jv-img-artifact-cap"
											><svg
												width="12"
												height="12"
												viewBox="0 0 24 24"
												fill="none"
												stroke="currentColor"
												stroke-width="1.8"
												stroke-linecap="round"
												stroke-linejoin="round"
											>
												<path
													d="M15 3h6v6M14 10l7-7M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"
												/></svg
											>{{ cv.title }}</span
										>
									</button>
									<!-- everything else → the file card -->
									<div v-else class="jv-artifact-group">
										<button
											class="jv-artifact"
											@click="openArtifact(m, cv)"
											:title="'Open ' + cv.title"
										>
											<span class="jv-artifact-ic" :class="'t-' + cv.type">
												<svg
													width="17"
													height="17"
													viewBox="0 0 24 24"
													fill="none"
													stroke="currentColor"
													stroke-width="1.7"
													stroke-linecap="round"
													stroke-linejoin="round"
												>
													<path
														d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"
													/>
													<path d="M14 2v6h6" />
												</svg>
											</span>
											<span class="jv-artifact-txt">
												<span class="jv-artifact-title">{{
													cv.title
												}}</span>
												<span class="jv-artifact-sub"
													>{{ (cv.type || "file").toUpperCase() }} · open
													preview</span
												>
											</span>
											<svg
												class="jv-artifact-go"
												width="15"
												height="15"
												viewBox="0 0 24 24"
												fill="none"
												stroke="currentColor"
												stroke-width="1.8"
												stroke-linecap="round"
												stroke-linejoin="round"
											>
												<path d="M9 18l6-6-6-6" />
											</svg>
										</button>
										<!-- a Dashboards build opened in main chat: the card
										     above previews the document but never runs its
										     queries — the builder does -->
										<button
											v-if="canOpenDash(cv)"
											class="jv-artifact-open"
											@click="openInDashboards(m, cv)"
											title="Open this dashboard in Dashboards, with its data"
										>
											<svg
												width="13"
												height="13"
												viewBox="0 0 24 24"
												fill="none"
												stroke="currentColor"
												stroke-width="1.9"
												stroke-linecap="round"
												stroke-linejoin="round"
											>
												<path d="M18 20V10M12 20V4M6 20v-6" />
											</svg>
											Open in Dashboards
										</button>
									</div>
								</template>
								<div v-if="skillsUsedOf(m).length" class="jv-skillused">
									<span
										v-for="(sk, si) in skillsUsedOf(m)"
										:key="si"
										class="jv-skillused-chip"
										:title="'This reply used the ' + sk + ' skill'"
										><svg
											width="11"
											height="11"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="1.9"
											stroke-linecap="round"
											stroke-linejoin="round"
										>
											<path d="M12 2 4 7v10l8 5 8-5V7z" />
											<path d="M12 22V12M12 12 4 7M12 12l8-5" /></svg
										>{{ sk }}</span
									>
								</div>
								<div class="jv-metabar">
									<div
										v-if="
											!m.error &&
											!m.streaming &&
											(toolCountOf(m) || elapsedOf(m))
										"
										class="jv-meta"
									>
										<span v-if="toolCountOf(m)" :title="activityNames(m.name)"
											><svg
												width="12"
												height="12"
												viewBox="0 0 24 24"
												fill="none"
												stroke="currentColor"
												stroke-width="1.8"
												stroke-linecap="round"
												stroke-linejoin="round"
											>
												<path
													d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 1 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"
												/></svg
											>{{ toolCountOf(m) }} tool{{
												toolCountOf(m) === 1 ? "" : "s"
											}}</span
										>
										<span v-if="elapsedOf(m)"
											><svg
												width="12"
												height="12"
												viewBox="0 0 24 24"
												fill="none"
												stroke="currentColor"
												stroke-width="1.8"
												stroke-linecap="round"
												stroke-linejoin="round"
											>
												<circle cx="12" cy="12" r="9" />
												<path d="M12 7v5l3 2" /></svg
											>{{ elapsedLabel(m) }}</span
										>
									</div>
									<!-- SUX-7: subtle "finishing…" affordance while the Relay-Pump
									     finalize job is still adding late enrichment (attachments /
									     canvas / title); cleared by the message:enriched event. -->
									<div
										v-if="!m.streaming && enrichmentPending.has(m.name)"
										class="jv-meta"
										style="opacity: 0.6"
										title="Finishing up — attachments and extras are still being added"
									>
										<span>Finishing…</span>
									</div>
									<div v-if="!m.error && m.content" class="jv-msgbar">
										<span
											v-if="msgTime(m)"
											class="jv-msgtime"
											:title="msgTimeFull(m)"
											>{{ msgTime(m) }}</span
										>
										<button
											class="jv-msgbtn"
											@click="copyMsg(m.name, stripBlocks(m.content))"
											:title="copiedId === m.name ? 'Copied' : 'Copy'"
										>
											<svg
												v-if="copiedId === m.name"
												width="14"
												height="14"
												viewBox="0 0 24 24"
												fill="none"
												stroke="var(--green)"
												stroke-width="2.2"
												stroke-linecap="round"
												stroke-linejoin="round"
											>
												<path d="M20 6 9 17l-5-5" />
											</svg>
											<svg
												v-else
												width="14"
												height="14"
												viewBox="0 0 24 24"
												fill="none"
												stroke="currentColor"
												stroke-width="1.8"
												stroke-linecap="round"
												stroke-linejoin="round"
											>
												<rect x="9" y="9" width="13" height="13" rx="2" />
												<path
													d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"
												/>
											</svg>
										</button>
									</div>
								</div>
							</template>
						</Message>
					</template>

					<!-- live tool activity + thinking (Claude Code style). Suppressed
					     while a queued chip is showing (F2): whenever the accept says
					     the turn is queued, the "Queued — ~N ahead" chip WINS over this
					     "Working on it…" / warming placeholder — never both, and never a
					     stray warming spinner masking the chip. -->
					<div
						v-if="(activeTools.length || waiting) && !queuedTurn"
						style="display: flex; gap: 12px"
					>
						<JarvisMark :size="28" :radius="7" style="margin-top: 2px" />
						<div style="flex: 1; min-width: 0; padding-top: 3px">
							<!-- the single tool running right now -->
							<div
								v-if="showActivityDetail && currentTool"
								:key="currentTool.id"
								class="jv-toolrow"
							>
								<svg
									class="jv-spin"
									width="13"
									height="13"
									viewBox="0 0 24 24"
									fill="none"
									stroke="var(--cta)"
									stroke-width="2.4"
									stroke-linecap="round"
								>
									<path d="M12 3a9 9 0 1 0 9 9" />
								</svg>
								<span
									>{{ toolPhrase(currentTool) }}
									<span
										style="
											font-family: ui-monospace, 'SF Mono', Menlo, monospace;
											font-size: 11px;
											color: var(--cta);
										"
										>{{ currentTool.name }}</span
									></span
								>
							</div>
							<!-- compact tally of tools finished this turn -->
							<div
								v-if="showActivityDetail && doneCount"
								class="jv-toolrow jv-tooldone"
							>
								<svg
									width="13"
									height="13"
									viewBox="0 0 24 24"
									fill="none"
									stroke="var(--green)"
									stroke-width="2.4"
									stroke-linecap="round"
									stroke-linejoin="round"
								>
									<path d="M20 6 9 17l-5-5" />
								</svg>
								<span
									>{{ doneCount }} tool{{
										doneCount === 1 ? "" : "s"
									}}
									done<template v-if="failedCount">
										· {{ failedCount }} failed</template
									></span
								>
							</div>
							<div
								v-if="
									!showActivityDetail ||
									(waiting && !currentTool) ||
									(!currentTool && statusPhase)
								"
								role="status"
								aria-live="polite"
								style="
									display: flex;
									align-items: center;
									gap: 7px;
									padding-top: 4px;
								"
							>
								<span style="display: flex; gap: 4px" aria-hidden="true">
									<span class="jv-tdot"></span>
									<span class="jv-tdot" style="animation-delay: 0.18s"></span>
									<span class="jv-tdot" style="animation-delay: 0.36s"></span>
								</span>
								<span style="font-size: 12px; color: var(--text-3)"
									>{{ liveStatus
									}}<span
										v-if="liveElapsedLabel"
										aria-hidden="true"
										style="opacity: 0.75"
									>
										· {{ liveElapsedLabel }}</span
									></span
								>
							</div>
						</div>
					</div>

					<!-- recovery: a parked turn finishing in the background (connection
					     hiccup / compaction). The composer stays UNLOCKED and the answer
					     lands later via the recovery path — fixes the silent limbo. -->
					<div v-if="recovering" style="display: flex; gap: 12px">
						<JarvisMark :size="28" :radius="7" style="margin-top: 2px" />
						<div style="flex: 1; min-width: 0; padding-top: 3px">
							<div
								role="status"
								aria-live="polite"
								style="
									display: flex;
									align-items: center;
									gap: 7px;
									font-size: 12px;
									color: var(--text-3);
								"
							>
								<svg
									class="jv-spin"
									width="13"
									height="13"
									viewBox="0 0 24 24"
									fill="none"
									stroke="var(--text-3)"
									stroke-width="2.4"
									stroke-linecap="round"
								>
									<path d="M12 3a9 9 0 1 0 9 9" />
								</svg>
								<span>{{ recoveringLabel }}</span>
							</div>
						</div>
					</div>

					<!-- SUX-3: immediate "change saved" acknowledgment after a confirm
					     write, so the card doesn't vanish into silence while the
					     continuation turn queues. Auto-clears (~3.5s). -->
					<div
						v-if="confirmedAck"
						role="status"
						aria-live="polite"
						style="
							display: flex;
							align-items: center;
							gap: 7px;
							font-size: 12px;
							color: var(--text-3);
							padding-left: 40px;
						"
					>
						<svg
							width="13"
							height="13"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="2.4"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M20 6 9 17l-5-5" />
						</svg>
						<span>Change saved</span>
					</div>

					<!-- Phase-0 admission (chat concurrency): the just-sent turn is
					     QUEUED behind others (all in-flight slots taken). The composer
					     stays unlocked; this chip shows the approximate position and a
					     Cancel affordance. Retired when the turn is promoted (run:start),
					     cancelled, or aged out. -->
					<div v-if="queuedTurn" style="display: flex; gap: 12px">
						<JarvisMark :size="28" :radius="7" style="margin-top: 2px" />
						<div style="flex: 1; min-width: 0; padding-top: 3px">
							<div
								role="status"
								aria-live="polite"
								style="
									display: flex;
									align-items: center;
									gap: 10px;
									font-size: 12px;
									color: var(--text-3);
								"
							>
								<svg
									class="jv-spin"
									width="13"
									height="13"
									viewBox="0 0 24 24"
									fill="none"
									stroke="var(--text-3)"
									stroke-width="2.4"
									stroke-linecap="round"
								>
									<path d="M12 3a9 9 0 1 0 9 9" />
								</svg>
								<span>{{
									queuedChipLabel(queuedTurn.position, queuedTurn.state)
								}}</span>
								<button
									type="button"
									class="jv-queued-cancel"
									aria-label="Cancel this queued message"
									@click="cancelQueued"
								>
									Cancel
								</button>
							</div>
						</div>
					</div>

					<!-- action:pending - gated ERP writes parked awaiting the owner's
					     Confirm click (issue #186). The authoritative confirm UI: each
					     carries its own server-minted one-time token. A single turn can
					     park more than one, so we stack a card per queued token (#4). -->
					<div
						v-for="pa in visiblePendingActions"
						:key="pa.token"
						class="jv-action jv-pending"
					>
						<div class="jv-action-head">
							<svg
								width="16"
								height="16"
								viewBox="0 0 24 24"
								fill="none"
								stroke="var(--amber)"
								stroke-width="1.8"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path
									d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"
								/>
								<path d="M12 9v4M12 17h.01" />
							</svg>
							<span class="jv-action-title">Confirm before this runs</span>
						</div>
						<div class="jv-pending-body">
							<div v-if="pendingSummaryOf(pa)" class="jv-pending-summary">
								{{ pendingSummaryOf(pa) }}
							</div>
							<div v-if="pendingExpiredOf(pa)" class="jv-pending-expired">
								This confirmation expired. Tell me the action again to retry it.
							</div>
							<template v-else>
								<!-- F9: the server-built "what will change" card (raw preview behind Details) -->
								<PendingCard
									v-if="pendingCardOf(pa)"
									:card="pendingCardOf(pa)"
									:details="pendingDetailsOf(pa)"
								/>
								<template v-else>
									<div v-if="pendingNoteOf(pa)" class="jv-pending-note">
										{{ pendingNoteOf(pa) }}
									</div>
									<ul v-if="pendingBatchOf(pa)" class="jv-pending-batch">
										<li
											v-for="(a, i) in pendingBatchOf(pa).actions"
											:key="'a' + i"
										>
											Create <b>{{ a.doctype }}</b> "{{ a.name }}"
										</li>
									</ul>
									<pre
										v-else-if="pendingPreviewOf(pa)"
										class="jv-pending-preview"
										>{{ pendingPreviewOf(pa) }}</pre
									>
								</template>
							</template>
						</div>
						<div v-if="pa.error" style="margin: 0 14px 10px">
							<ActionError :error="pa.error" />
						</div>
						<div class="jv-action-foot">
							<button
								class="jv-action-primary"
								:disabled="pa.busy || convStreaming || pendingExpiredOf(pa)"
								:title="
									convStreaming ? 'Waiting for the current reply to finish' : ''
								"
								@click="confirmPending(pa)"
							>
								<span v-if="pa.busy">Confirming…</span
								><span v-else>✓ Confirm</span>
							</button>
							<button
								class="jv-action-discard"
								:disabled="pa.busy"
								@click="discardPending(pa)"
							>
								Discard
							</button>
						</div>
					</div>
				</div>
			</div>

			<!-- ===== COMPOSER ===== -->
			<div
				class="jv-composer-wrap"
				style="
					position: relative;
					flex: none;
					padding: 12px 40px 16px;
					border-top: 1px solid var(--border);
					background: var(--surface);
				"
			>
				<!-- Billing lifecycle: before expiry, during grace, and after.
					 Replaces (not stacks with) the suspended banner - its copy is
					 the admin wording, which is wrong for a member who cannot
					 renew. -->
				<Banner
					v-if="billingAlert"
					:type="billingAlert.type"
					:title="billingAlert.title"
					:message="billingAlert.message"
					style="margin-bottom: 10px"
				>
					<template #action>
						<button
							v-if="billingAlert.showRenew"
							class="jv-btn jv-btn--sm"
							@click="goRenew"
						>
							Renew
						</button>
						<button
							v-if="billingAlert.dismissible"
							class="jv-btn jv-btn--sm jv-btn--ghost"
							@click="dismissBillingAlert"
						>
							Dismiss
						</button>
					</template>
				</Banner>
				<!-- Lapsed sub: container stopped, so every send would fail. -->
				<Banner
					v-else-if="suspendedNotice"
					type="warning"
					title="Chat is paused"
					:message="suspendedNotice"
					style="margin-bottom: 10px"
				>
					<template #action>
						<button class="jv-btn jv-btn--sm" @click="goRenew">Renew</button>
					</template>
				</Banner>
				<!-- No model configured: the workspace was disconnected, or its credential
					 expired. The composer is disabled alongside this (see canSend) - every
					 send would fail at the agent, and letting it be tried just turns a clear
					 explanation into an error the customer has to interpret.

					 ORDER MATTERS, and it is pinned by readiness.spec.js. This banner is the
					 SPECIFIC one and carries the only route back to the AI models pane, so it
					 must be tested before the generic notReadyNotice below or a generic,
					 CTA-less banner shadows it. readiness.js keeps the two verdicts mutually
					 exclusive as well (llm_credentials belongs to needsLlmConnection alone),
					 so this cannot swallow an unrelated not-ready state such as
					 container_provisioning either. -->
				<Banner
					v-else-if="noAiConnected"
					type="warning"
					title="No AI connected"
					message="Connect a model to start chatting again"
					style="margin-bottom: 10px"
				>
					<template #action>
						<button
							v-if="canConnectModel"
							class="jv-btn jv-btn--sm"
							@click="goConnectModel"
						>
							Connect a model
						</button>
					</template>
				</Banner>
				<!-- Not chat-ready for a NON-billing reason (e.g. the connected LLM account
					 itself is out of quota, or a container is still coming up). No CTA: unlike
					 a lapsed subscription there's nothing to renew via us here - the detail
					 IS the admin's own diagnosis (jarvis.account.is_ready_for_chat), not a
					 guess this UI is making up. -->
				<Banner
					v-else-if="notReadyNotice"
					type="warning"
					title="Chat may not work yet"
					:message="notReadyNotice"
					style="margin-bottom: 10px"
				/>

				<!-- floats just above the composer; jumps the thread to the newest message -->
				<transition name="jv-sd">
					<button
						v-if="showScrollDown && !showWelcome && !booting"
						class="jv-scrolldown"
						@click="jumpToBottom"
						title="Jump to latest"
						aria-label="Jump to latest message"
					>
						<svg
							width="18"
							height="18"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="2.2"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M12 5v14" />
							<path d="m19 12-7 7-7-7" />
						</svg>
					</button>
				</transition>
				<div style="max-width: 1280px; margin: 0 auto">
					<!-- DO NOT REMOVE `@submit="send()"`: the Send BUTTON's click is
					     `emit('submit')` inside Composer, so this binding is the
					     button's ONLY path to send(). (It is Composer's built-in
					     Enter→submit that is unreachable for chat — onKey
					     preventDefaults on Enter and calls send() itself — not the
					     click.) Composer's own test asserts the emit, not this
					     wiring, so deleting it would kill click-to-send silently. -->
					<Composer
						ref="composerRef"
						v-model="input"
						:attachments="composerAttachments"
						:busy="busy"
						:canSend="canSend"
						:sendTitle="voiceSendBlockReason"
						:placeholder="`Ask ${agentName}…   @ to mention a user, / for a doctype or tool`"
						:disclaimer="`${agentName} can make mistakes. Verify important actions before submitting to ERPNext.`"
						@submit="send()"
						@stop="stopRun"
						@input="onInput"
						@keydown="onKey"
						@paste="onPaste"
						@files-added="uploadFiles"
						@remove-attachment="removeFile"
					>
						<template #above>
							<!-- wiki nudge: "anything worth remembering?" card (realtime wiki:nudge
							     event, §voice/wiki). Own block ABOVE the composer so it never
							     shifts the input while a reply is streaming. -->
							<div
								v-if="nudge && nudge.conversationId === currentId"
								class="jv-nudge"
							>
								<div class="jv-nudge-head">
									<div class="jv-nudge-q">
										Anything worth remembering about <b>{{ nudgeLabels }}</b
										>?
									</div>
									<button
										class="jv-nudge-x"
										title="Dismiss"
										aria-label="Dismiss"
										@click="dismissNudge"
									>
										<svg
											width="13"
											height="13"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="2"
											stroke-linecap="round"
											stroke-linejoin="round"
										>
											<path d="M18 6 6 18M6 6l12 12" />
										</svg>
									</button>
								</div>
								<div v-if="nudge.mode !== 'edit'" class="jv-nudge-actions">
									<template v-if="ui.stt_enabled && nudgeRec.supported">
										<button
											v-if="nudge.mode === 'transcribing'"
											class="jv-iconbtn jv-micbtn"
											title="Transcribing…"
											disabled
											style="
												width: 28px;
												height: 28px;
												display: flex;
												align-items: center;
												justify-content: center;
												background: transparent;
												border: none;
												border-radius: 7px;
												color: var(--text-3);
											"
										>
											<svg
												class="jv-spin"
												width="14"
												height="14"
												viewBox="0 0 24 24"
												fill="none"
												stroke="var(--cta)"
												stroke-width="2.4"
												stroke-linecap="round"
											>
												<path d="M12 3a9 9 0 1 0 9 9" />
											</svg>
										</button>
										<!-- labeled, unlike the composer's dictate mic 40px below — two
										     identical icon-only mics were indistinguishable at a glance -->
										<button
											v-else
											class="jv-iconbtn jv-micbtn"
											:class="{ rec: nudge.mode === 'recording' }"
											:title="
												nudge.mode === 'recording'
													? 'Stop and transcribe'
													: `Record a voice note (saved for ${agentName} to learn from)`
											"
											@click="
												nudge.mode === 'recording'
													? stopNudgeMic()
													: startNudgeMic()
											"
											style="
												height: 28px;
												display: flex;
												align-items: center;
												justify-content: center;
												gap: 5px;
												background: transparent;
												border: none;
												border-radius: 7px;
												cursor: pointer;
												color: var(--text-3);
												padding: 0 8px;
												font-size: 12.5px;
												font-weight: 600;
											"
										>
											<svg
												width="15"
												height="15"
												viewBox="0 0 24 24"
												fill="none"
												stroke="currentColor"
												stroke-width="1.7"
												stroke-linecap="round"
												stroke-linejoin="round"
											>
												<path
													d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"
												/>
												<path d="M19 10v2a7 7 0 0 1-14 0v-2" />
												<path d="M12 19v3" />
											</svg>
											<span v-if="nudge.mode !== 'recording'"
												>Record answer</span
											>
										</button>
										<template v-if="nudge.mode === 'recording'">
											<span class="jv-mic-live"
												><span class="jv-mic-dot"></span
												>{{ nudgeClock }}</span
											>
											<button
												class="jv-mic-cancel"
												title="Cancel recording (Esc)"
												aria-label="Cancel recording"
												@click="cancelNudgeMic"
											>
												<svg
													width="12"
													height="12"
													viewBox="0 0 24 24"
													fill="none"
													stroke="currentColor"
													stroke-width="2.2"
													stroke-linecap="round"
													stroke-linejoin="round"
												>
													<path d="M18 6 6 18M6 6l12 12" />
												</svg>
											</button>
										</template>
									</template>
									<button
										v-if="nudge.mode === 'idle'"
										class="jv-nudge-type"
										@click="typeNudge"
									>
										Type instead
									</button>
								</div>
								<template v-else>
									<textarea
										ref="nudgeTaEl"
										v-model="nudge.text"
										rows="3"
										class="jv-nudge-ta"
										:placeholder="`What should ${agentName} remember?`"
									></textarea>
									<div class="jv-nudge-foot">
										<button
											class="jv-btn jv-btn--ghost"
											style="height: 30px; padding: 0 12px"
											:disabled="nudge.saving"
											@click="nudge.mode = 'idle'"
										>
											Cancel
										</button>
										<button
											class="jv-btn jv-btn--primary"
											style="height: 30px; padding: 0 12px"
											:disabled="nudge.saving || !nudge.text.trim()"
											@click="saveNudgeNote"
										>
											{{ nudge.saving ? "Saving…" : "Save" }}
										</button>
									</div>
								</template>
							</div>
						</template>
						<template #overlay>
							<!-- mention dropdown (@ user, / doctype·tool) -->
							<div
								v-if="mention.open && mention.items.length"
								style="
									position: absolute;
									bottom: calc(100% + 6px);
									left: 0;
									min-width: 248px;
									max-height: 248px;
									overflow-y: auto;
									background: var(--surface);
									border: 1px solid var(--border-2);
									border-radius: 10px;
									box-shadow: 0 10px 28px rgba(20, 20, 30, 0.16);
									padding: 5px;
									z-index: 30;
								"
							>
								<button
									v-for="(it, i) in mention.items"
									:key="it.value"
									class="jv-menuitem"
									:class="{ on: i === mention.index }"
									@click="applyMention(it)"
									@mouseenter="mention.index = i"
								>
									<span
										style="
											flex: 1;
											overflow: hidden;
											text-overflow: ellipsis;
											white-space: nowrap;
										"
										>{{ mention.type }}{{ it.value }}</span
									>
									<span
										style="
											font-size: 10px;
											color: var(--text-3);
											text-transform: uppercase;
											letter-spacing: 0.03em;
										"
										>{{ it.sub }}</span
									>
								</button>
							</div>
							<!-- clipboard held only a file PATH, not the image bytes -->
							<div
								v-if="pasteHint"
								style="
									font-size: 11.5px;
									color: var(--amber);
									padding: 4px 8px;
									line-height: 1.4;
								"
							>
								{{ pasteHint }}
							</div>
						</template>
						<template #below-input>
							<!-- transcribing pill: ONE per recording, right where its words will
						     land. M:SS is the RECORDING's length — there is no progress to
						     report and inventing one would be theatre. Past 10 s it also states
						     how long it has been waiting (a real number), and Cancel ends the
						     wait: the recording drops to its failed chip with the audio intact,
						     which is what un-blocks sending. -->
							<div
								v-if="ui.stt_enabled && voiceQ.transcribing.length"
								style="
									display: flex;
									flex-wrap: wrap;
									align-items: center;
									gap: 6px;
									margin: 2px 4px 6px;
								"
							>
								<span
									v-for="t in voiceQ.transcribing"
									:key="'tx-' + t.id"
									style="
										display: inline-flex;
										align-items: center;
										gap: 6px;
										padding: 3px 8px;
										border-radius: 999px;
										font-size: 12px;
										color: var(--text-3);
										background: rgba(127, 127, 127, 0.1);
										border: 1px solid rgba(127, 127, 127, 0.22);
									"
								>
									<svg
										class="jv-spin"
										width="12"
										height="12"
										viewBox="0 0 24 24"
										fill="none"
										stroke="var(--cta)"
										stroke-width="2.6"
										stroke-linecap="round"
									>
										<path d="M12 3a9 9 0 1 0 9 9" />
									</svg>
									Transcribing {{ recLength(t.durationS) }}…{{
										transcribingWait(t)
									}}
									<button
										class="jv-voicechip-quiet"
										title="Stop waiting for this transcription — the recording is kept, with Retry and Download"
										aria-label="Cancel this transcription"
										@click="cancelTranscribing(t.id)"
									>
										Cancel
									</button>
								</span>
							</div>
							<!-- recovery banner: a prior session left a recording un-transcribed. ONE
						     row per recording, each with its own length and its own actions. HIDDEN
						     while recording — the composer is already filling from a live take. A
						     recording whose FIRST fragment is missing can't be rebuilt into playable
						     audio, so it offers only Download (raw bytes) and Discard: never a
						     silent loss, and never a Transcribe button that can't work. -->
							<template v-if="ui.stt_enabled && micState !== 'recording'">
								<div
									v-for="t in recoveryTakes"
									:key="'rec-' + t.recordingId"
									style="
										display: flex;
										flex-wrap: wrap;
										align-items: center;
										gap: 8px;
										margin: 2px 4px 6px;
										padding: 6px 10px;
										border-radius: 8px;
										font-size: 12px;
										color: var(--text);
										background: rgba(127, 127, 127, 0.1);
										border: 1px solid rgba(127, 127, 127, 0.22);
									"
								>
									<span>{{ recoveryLabel(t) }}</span>
									<button
										v-if="t.complete"
										class="jv-voicechip-act"
										@click="recoverTake(t)"
									>
										Transcribe
									</button>
									<button class="jv-voicechip-act" @click="downloadTake(t)">
										Download
									</button>
									<!-- non-destructive escape: hide it for this tab session, keep
								     the audio. Without it the only ways out are to act now or see
								     the banner on every reload — pressure toward the one button
								     that deletes. -->
									<button class="jv-voicechip-act" @click="laterTake(t)">
										Later
									</button>
									<!-- destructive: quieter weight + confirm before deleting audio -->
									<button
										class="jv-voicechip-quiet"
										title="Permanently delete the saved audio"
										@click="discardTake(t)"
									>
										Discard
									</button>
								</div>
							</template>
							<!-- failed-recording chip: the take exhausted its transcription budget. The
						     audio is retained, so Retry re-sends the SAME bytes and Download saves
						     them, and the label carries the server's reason so a permanent fault
						     is distinguishable from a blip. Once a message has gone out without it
						     the label + Retry tooltip switch to the post-send wording — the two
						     states are otherwise indistinguishable and the chip reads as stuck
						     clutter. A take nothing was heard in (measured silent here, or an
						     empty transcript from the server) lands here too, reading "nothing was
						     heard" with "Transcribe anyway": a silence measurement is never
						     allowed to delete a recording, only the user is. -->
							<div
								v-if="ui.stt_enabled && voiceQ.failed.length"
								style="
									display: flex;
									flex-wrap: wrap;
									align-items: center;
									gap: 6px;
									margin: 2px 4px 6px;
								"
							>
								<span
									v-for="f in voiceQ.failed"
									:key="'fail-' + f.id"
									:title="failedChipTitle(f)"
									style="
										display: inline-flex;
										align-items: center;
										gap: 6px;
										padding: 3px 8px;
										border-radius: 999px;
										font-size: 12px;
										color: var(--text);
										background: rgba(220, 150, 40, 0.12);
										border: 1px solid rgba(220, 150, 40, 0.32);
									"
								>
									{{ failedChipLabel(f) }}
									<button
										class="jv-voicechip-act"
										:title="failedChipRetryTitle(f)"
										@click="retryRecording(f.id)"
									>
										{{ failedChipRetryLabel(f) }}
									</button>
									<button
										class="jv-voicechip-act"
										@click="downloadRecording(f.id)"
									>
										Download
									</button>
									<button
										class="jv-voicechip-quiet"
										title="Discard this recording"
										aria-label="Discard this recording"
										@click="discardRecording(f.id)"
									>
										✕
									</button>
								</span>
							</div>
							<!-- retained-recording chip: a transcribed recording whose words were edited
						     OUT of the message before sending. Its audio is retained (never lost) but
						     was otherwise actionless — Restore drops the words back into the composer,
						     Download saves the audio, ✕ discards it (and clears the leave guard). -->
							<div
								v-if="ui.stt_enabled && voiceQ.retained && voiceQ.retained.length"
								style="
									display: flex;
									flex-wrap: wrap;
									align-items: center;
									gap: 6px;
									margin: 2px 4px 6px;
								"
							>
								<span
									v-for="r in voiceQ.retained"
									:key="'ret-' + r.id"
									style="
										display: inline-flex;
										align-items: center;
										gap: 6px;
										padding: 3px 8px;
										border-radius: 999px;
										font-size: 12px;
										color: var(--text);
										background: rgba(90, 130, 200, 0.12);
										border: 1px solid rgba(90, 130, 200, 0.32);
									"
								>
									Recording {{ recLength(r.durationS) }} was edited out
									<button
										class="jv-voicechip-act"
										title="Put this recording's transcript back into the message"
										@click="restoreRecording(r.id)"
									>
										Restore
									</button>
									<button
										class="jv-voicechip-act"
										@click="downloadRecording(r.id)"
									>
										Download
									</button>
									<button
										class="jv-voicechip-quiet"
										title="Discard this recording"
										aria-label="Discard this recording"
										@click="discardRetainedRecording(r.id)"
									>
										✕
									</button>
								</span>
							</div>
							<!-- unsaved-audio chip: the recording could NOT be durably saved on this
						     device (storage full / private mode / IndexedDB blocked). Recording is
						     stopped; a crash or reload would otherwise lose this audio silently, so
						     it is surfaced as ACTIONABLE — Download keeps it, Retry re-attempts the
						     save once space is freed, ✕ drops it. Without that ✕ the chip had no
						     exit at all: it also arms the leave guard on every navigation, and
						     "send the message" / "free storage and Retry" are not affordances. -->
							<div
								v-if="
									ui.stt_enabled &&
									voiceQ.unpersisted &&
									voiceQ.unpersisted.length
								"
								style="
									display: flex;
									flex-wrap: wrap;
									align-items: center;
									gap: 6px;
									margin: 2px 4px 6px;
								"
							>
								<span
									v-for="u in voiceQ.unpersisted"
									:key="'unp-' + u.id"
									style="
										display: inline-flex;
										align-items: center;
										gap: 6px;
										padding: 3px 8px;
										border-radius: 999px;
										font-size: 12px;
										color: var(--text);
										background: rgba(210, 70, 70, 0.12);
										border: 1px solid rgba(210, 70, 70, 0.34);
									"
								>
									Recording {{ recLength(u.durationS) }} couldn't be saved on
									this device
									<button
										class="jv-voicechip-act"
										title="Try saving this recording's audio again (e.g. after freeing storage)"
										@click="retryPersistRecording(u.id)"
									>
										Retry
									</button>
									<button
										class="jv-voicechip-act"
										@click="downloadRecording(u.id)"
									>
										Download
									</button>
									<button
										class="jv-voicechip-quiet"
										title="Discard this recording"
										aria-label="Discard this recording"
										@click="discardUnpersistedRecording(u.id)"
									>
										✕
									</button>
								</span>
							</div>
						</template>
						<template #left-toolbar="{ pickFiles }">
							<!-- dictation mic (hidden unless the backend reports STT configured) -->
							<template v-if="ui.stt_enabled && micRec.supported">
								<button
									class="jv-iconbtn jv-micbtn"
									:class="{ rec: micState === 'recording' }"
									:title="
										micState === 'recording'
											? 'Stop dictation'
											: 'Dictate (voice to text)'
									"
									@click="micState === 'recording' ? stopMic() : startMic()"
									style="
										width: 30px;
										height: 30px;
										display: flex;
										align-items: center;
										justify-content: center;
										background: transparent;
										border: none;
										border-radius: 7px;
										cursor: pointer;
										color: var(--text-3);
									"
								>
									<svg
										width="17"
										height="17"
										viewBox="0 0 24 24"
										fill="none"
										stroke="currentColor"
										stroke-width="1.7"
										stroke-linecap="round"
										stroke-linejoin="round"
									>
										<path
											d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"
										/>
										<path d="M19 10v2a7 7 0 0 1-14 0v-2" />
										<path d="M12 19v3" />
									</svg>
								</button>
								<template v-if="micState === 'recording'">
									<span class="jv-mic-live"
										><span class="jv-mic-dot"></span>{{ micClock }}</span
									>
									<button
										class="jv-mic-cancel"
										title="Cancel recording (Esc) — throws this whole recording away; text already in the box is kept"
										aria-label="Cancel recording"
										@click="cancelMic"
									>
										<svg
											width="12"
											height="12"
											viewBox="0 0 24 24"
											fill="none"
											stroke="currentColor"
											stroke-width="2.2"
											stroke-linecap="round"
											stroke-linejoin="round"
										>
											<path d="M18 6 6 18M6 6l12 12" />
										</svg>
									</button>
								</template>
								<!-- the transcribing indicator lives in #below-input, where the words
							     will actually land — one pill per recording, not a count here. -->
							</template>
							<button
								class="jv-iconbtn"
								title="Attach file"
								@click="pickFiles"
								style="
									width: 30px;
									height: 30px;
									display: flex;
									align-items: center;
									justify-content: center;
									background: transparent;
									border: none;
									border-radius: 7px;
									cursor: pointer;
									color: var(--text-3);
								"
							>
								<svg
									width="17"
									height="17"
									viewBox="0 0 24 24"
									fill="none"
									stroke="currentColor"
									stroke-width="1.7"
									stroke-linecap="round"
									stroke-linejoin="round"
								>
									<path
										d="M21.44 11.05l-9.19 9.19a5 5 0 0 1-7.07-7.07l9.19-9.19a3.5 3.5 0 0 1 4.95 4.95l-9.2 9.19a1.5 1.5 0 0 1-2.12-2.12l8.49-8.49"
									/>
								</svg>
							</button>
							<button
								v-if="ui.wiki_enabled"
								class="jv-iconbtn"
								:title="
									groundNextTurn
										? 'Wiki grounding armed — your next message will be answered from the wiki (click to turn off)'
										: 'Ground your next message on the org wiki'
								"
								@click="groundNextTurn = !groundNextTurn"
								:aria-pressed="String(groundNextTurn)"
								:style="{
									height: '30px',
									display: 'flex',
									alignItems: 'center',
									gap: '4px',
									padding: groundNextTurn ? '0 8px' : '0',
									width: groundNextTurn ? 'auto' : '30px',
									justifyContent: 'center',
									background: 'transparent',
									border: groundNextTurn ? '1px solid var(--cta)' : 'none',
									borderRadius: '7px',
									cursor: 'pointer',
									color: groundNextTurn ? 'var(--cta)' : 'var(--text-3)',
									fontSize: '12px',
									fontWeight: '500',
								}"
							>
								<svg
									width="16"
									height="16"
									viewBox="0 0 24 24"
									fill="none"
									stroke="currentColor"
									stroke-width="1.7"
									stroke-linecap="round"
									stroke-linejoin="round"
								>
									<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
									<path
										d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"
									/>
								</svg>
								<span v-if="groundNextTurn">Wiki</span>
							</button>
							<!-- "Get help from a human" — always-available chat hook into the
							     standalone support space (Task 6). Gated on the same dual
							     kill-switch the /support routes guard on. -->
							<button
								v-if="supportOn"
								class="jv-iconbtn"
								title="Get help from a human"
								@click="getHumanHelp"
								style="
									width: 30px;
									height: 30px;
									display: flex;
									align-items: center;
									justify-content: center;
									background: transparent;
									border: none;
									border-radius: 7px;
									cursor: pointer;
									color: var(--text-3);
								"
							>
								<svg
									width="17"
									height="17"
									viewBox="0 0 24 24"
									fill="none"
									stroke="currentColor"
									stroke-width="1.7"
									stroke-linecap="round"
									stroke-linejoin="round"
								>
									<circle cx="12" cy="12" r="10" />
									<circle cx="12" cy="12" r="4" />
									<line x1="4.93" y1="4.93" x2="9.17" y2="9.17" />
									<line x1="14.83" y1="14.83" x2="19.07" y2="19.07" />
									<line x1="14.83" y1="9.17" x2="19.07" y2="4.93" />
									<line x1="4.93" y1="19.07" x2="9.17" y2="14.83" />
								</svg>
							</button>
						</template>
					</Composer>
				</div>
			</div>
		</main>

		<!-- ============ PROACTIVE MESSAGE TOAST (Jarvis started a chat) ============ -->
		<transition name="jv-fade">
			<div v-if="proactiveToast" class="jv-toast" @click="openProactive">
				<JarvisMark :size="30" :radius="8" />
				<div style="min-width: 0; flex: 1">
					<div class="jv-toast-title">{{ proactiveToast.title }}</div>
					<div class="jv-toast-preview">{{ proactiveToast.preview }}</div>
				</div>
				<button class="jv-toast-open" @click.stop="openProactive">Open</button>
				<button class="jv-toast-x" @click.stop="proactiveToast = null" title="Dismiss">
					<svg
						width="14"
						height="14"
						viewBox="0 0 24 24"
						fill="none"
						stroke="currentColor"
						stroke-width="2"
						stroke-linecap="round"
						stroke-linejoin="round"
					>
						<path d="M18 6 6 18M6 6l12 12" />
					</svg>
				</button>
			</div>
		</transition>

		<!-- screen-reader-only announcements: turn completion / failure -->
		<div class="jv-sr" role="status" aria-live="polite">{{ srMessage }}</div>
		<!-- ============ NOTIFIER (reusable toasts: "Deleted", etc.) ============ -->
		<div class="jv-notes" aria-live="polite">
			<transition-group name="jv-note">
				<div v-for="n in notes" :key="n.id" class="jv-note" :class="n.type" role="status">
					<span class="jv-note-ic" aria-hidden="true">
						<svg
							v-if="n.type === 'success'"
							width="14"
							height="14"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="2.6"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M20 6 9 17l-5-5" />
						</svg>
						<svg
							v-else-if="n.type === 'error'"
							width="14"
							height="14"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="2.4"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<circle cx="12" cy="12" r="9" />
							<path d="M12 8v5M12 16.4v.01" />
						</svg>
						<svg
							v-else
							width="14"
							height="14"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="2.4"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<circle cx="12" cy="12" r="9" />
							<path d="M12 16v-5M12 8v.01" />
						</svg>
					</span>
					<div class="jv-note-body">
						<div v-if="n.title" class="jv-note-title">{{ n.title }}</div>
						<div class="jv-note-msg">{{ n.message }}</div>
					</div>
					<button
						class="jv-note-x"
						@click="dismissNote(n.id)"
						title="Dismiss"
						aria-label="Dismiss"
					>
						<svg
							width="12"
							height="12"
							viewBox="0 0 24 24"
							fill="none"
							stroke="currentColor"
							stroke-width="2"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M18 6 6 18M6 6l12 12" />
						</svg>
					</button>
				</div>
			</transition-group>
		</div>

		<!-- CONFIRM DIALOG moved to the app shell (components/shell/ConfirmDialog.vue),
		     driven by composables/useConfirm.js — call confirm() from anywhere. -->

		<!-- SETTINGS dialog hoisted to the shell (components/shell/SettingsDialog.vue) -->

		<!-- Artifact preview panel — slides in from the right (PDF in a viewer, Excel as a table) -->
		<transition name="jv-slide">
			<div
				v-if="artifact"
				class="jv-artifact-overlay"
				@mousedown="onOverlayMouseDown"
				@click.self="onOverlayBackdropClick(closeArtifact)"
			>
				<aside class="jv-artifact-panel" ref="artifactPanelEl" tabindex="-1">
					<div class="jv-artifact-head">
						<svg
							width="15"
							height="15"
							viewBox="0 0 24 24"
							fill="none"
							stroke="var(--text-3)"
							stroke-width="1.8"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
							<path d="M14 2v6h6" />
						</svg>
						<span class="jv-artifact-head-title">{{ artifact.cv.title }}</span>
						<span class="jv-canvas-type">{{ artifact.cv.type }}</span>
						<a
							class="jv-art-act"
							:href="artifact.url"
							target="_blank"
							rel="noopener"
							title="Open in new tab"
							aria-label="Open in new tab"
						>
							<svg
								width="16"
								height="16"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="1.8"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path
									d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14 21 3"
								/>
							</svg>
						</a>
						<a
							class="jv-art-act"
							:href="artifact.url"
							:download="cvFile(artifact.cv)"
							title="Download"
							aria-label="Download"
						>
							<svg
								width="16"
								height="16"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="1.8"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path
									d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"
								/>
							</svg>
						</a>
						<!-- the same hand-off as the inline card, for the artifact that is
						     already open: this preview does not run the dashboard. The
						     conversation check is not belt-and-braces: this panel survives
						     a switch in the sidebar behind it, and the hand-off pairs the
						     CURRENT conversation with the OPEN artifact's message -->
						<button
							v-if="canOpenDash(artifact.cv) && artifact.conv === currentId"
							class="jv-art-act"
							@click="openInDashboards(artifact.m, artifact.cv)"
							title="Open in Dashboards"
							aria-label="Open in Dashboards"
						>
							<svg
								width="16"
								height="16"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="1.8"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path d="M18 20V10M12 20V4M6 20v-6" />
							</svg>
						</button>
						<span class="jv-art-divider" aria-hidden="true"></span>
						<button
							class="jv-art-close"
							@click="closeArtifact"
							title="Close preview (Esc)"
							aria-label="Close preview"
						>
							<svg
								width="17"
								height="17"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="1.9"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path d="M18 6 6 18M6 6l12 12" />
							</svg>
						</button>
					</div>
					<div class="jv-artifact-body">
						<iframe
							v-if="artifact.kind === 'pdf'"
							:src="artifact.url"
							class="jv-artifact-frame"
							title="PDF preview"
						></iframe>
						<img
							v-else-if="artifact.kind === 'image'"
							:src="artifact.url"
							class="jv-artifact-img"
							:alt="artifact.cv.title"
						/>
						<iframe
							v-else-if="artifact.kind === 'html' || artifact.kind === 'svg'"
							:srcdoc="artifact.content"
							sandbox="allow-scripts"
							class="jv-artifact-frame"
							title="Artifact preview"
						></iframe>
						<template v-else-if="artifact.kind === 'table'">
							<div v-if="artifact.sheets.length > 1" class="jv-sheet-tabs">
								<button
									v-for="(sh, si) in artifact.sheets"
									:key="si"
									:class="{ on: si === artifact.sheetIdx }"
									@click="setSheet(si)"
								>
									{{ sh.name }}
								</button>
							</div>
							<div class="jv-sheet-scroll">
								<table class="jv-sheet">
									<thead v-if="curSheet.rows.length">
										<tr>
											<th v-for="(c, ci) in curSheet.rows[0]" :key="ci">
												{{ c }}
											</th>
										</tr>
									</thead>
									<tbody>
										<tr v-for="(row, ri) in curSheet.rows.slice(1)" :key="ri">
											<td v-for="(c, ci) in row" :key="ci">{{ c }}</td>
										</tr>
									</tbody>
								</table>
							</div>
						</template>
						<pre v-else-if="artifact.kind === 'text'" class="jv-artifact-text">{{
							artifact.text
						}}</pre>
						<div v-else-if="artifact.kind === 'loading'" class="jv-artifact-loading">
							Loading preview…
						</div>
						<div v-else class="jv-artifact-nopreview">
							<p class="jv-preview-note">No inline preview for this file type.</p>
							<a
								:href="artifact.url"
								:download="cvFile(artifact.cv)"
								class="jv-canvas-dl"
								>Download {{ cvFile(artifact.cv) }}</a
							>
						</div>
					</div>
				</aside>
			</div>
		</transition>
		<!-- Record draft panel — the agent's proposed create/update, fully editable, applied directly -->
		<transition name="jv-slide">
			<div
				v-if="draftPanel"
				class="jv-artifact-overlay"
				@mousedown="onOverlayMouseDown"
				@click.self="onOverlayBackdropClick(closeDraftPanel)"
			>
				<aside
					ref="draftPanelEl"
					class="jv-artifact-panel jv-draft-panel"
					:class="{ 'jv-draft-panel-resizing': draftResizing }"
					:style="{ width: draftPanelWidth + 'px' }"
					tabindex="-1"
				>
					<div
						class="jv-draft-resizer"
						:class="{ 'jv-draft-resizer-active': draftResizing }"
						title="Drag to resize"
						@mousedown.prevent="startDraftResize"
					/>
					<div class="jv-artifact-head">
						<svg
							width="15"
							height="15"
							viewBox="0 0 24 24"
							fill="none"
							stroke="var(--text-3)"
							stroke-width="1.8"
							stroke-linecap="round"
							stroke-linejoin="round"
						>
							<rect x="3" y="4" width="18" height="16" rx="2" />
							<path d="M3 10h18M9 4v16" />
						</svg>
						<span class="jv-artifact-head-title"
							>{{ draftPanel.verb === "update" ? "Update" : "New" }}
							{{ draftPanel.doctype
							}}<template v-if="draftPanel.docName">
								· {{ draftPanel.docName }}</template
							></span
						>
						<span class="jv-draft-badge">Draft - not saved</span>
						<button
							class="jv-art-close"
							@click="closeDraftPanel"
							title="Close (draft stays in chat)"
							aria-label="Close"
						>
							<svg
								width="17"
								height="17"
								viewBox="0 0 24 24"
								fill="none"
								stroke="currentColor"
								stroke-width="1.9"
								stroke-linecap="round"
								stroke-linejoin="round"
							>
								<path d="M18 6 6 18M6 6l12 12" />
							</svg>
						</button>
					</div>
					<div class="jv-draft-body">
						<div v-if="draftPanel.updatedToast" class="jv-draft-toast">
							Draft updated from chat
						</div>
						<div class="jv-draft-fields">
							<div
								v-for="f in draftPanel.fields"
								:key="f.fieldname"
								class="jv-draft-fld"
								:class="{
									missing: f.reqd && !String(f.value).trim(),
									changed: f.changed,
								}"
							>
								<label
									>{{ f.label
									}}<span v-if="f.reqd" class="jv-req"> *</span></label
								>
								<div class="jv-draft-ctl">
									<template v-if="f.control === 'link'">
										<input
											class="jv-action-input"
											v-model="f.value"
											@input="
												onDraftLink(
													'f:' + f.fieldname,
													() => f.value,
													f.options,
													$event
												)
											"
											@focus="
												onDraftLink(
													'f:' + f.fieldname,
													() => f.value,
													f.options,
													$event
												)
											"
											@blur="closeDraftLink"
											:placeholder="
												'Search ' + (f.options || 'records') + '…'
											"
											autocomplete="off"
										/>
										<div
											v-if="
												draftLink.open &&
												draftLink.key === 'f:' + f.fieldname &&
												draftLink.items.length
											"
											class="jv-action-linkmenu"
											:class="{ up: draftLink.up }"
										>
											<button
												v-for="it in draftLink.items"
												:key="it.value"
												@mousedown.prevent="
													pickDraftLink((v) => {
														f.value = v;
													}, it)
												"
											>
												<b>{{ it.value }}</b
												><span v-if="it.label"> — {{ it.label }}</span>
											</button>
										</div>
									</template>
									<select
										v-else-if="f.control === 'select'"
										class="jv-action-input jv-action-sel"
										v-model="f.value"
									>
										<option v-for="o in f.options" :key="o" :value="o">
											{{ o }}
										</option>
									</select>
									<select
										v-else-if="f.control === 'check'"
										class="jv-action-input jv-action-sel"
										v-model="f.value"
									>
										<option>Yes</option>
										<option>No</option>
									</select>
									<input
										v-else-if="f.control === 'date'"
										type="date"
										class="jv-action-input"
										v-model="f.value"
									/>
									<input
										v-else-if="f.control === 'datetime'"
										type="datetime-local"
										class="jv-action-input"
										v-model="f.value"
									/>
									<input
										v-else-if="f.control === 'time'"
										type="time"
										class="jv-action-input"
										v-model="f.value"
									/>
									<input
										v-else-if="f.control === 'number'"
										type="number"
										class="jv-action-input"
										v-model="f.value"
									/>
									<textarea
										v-else-if="f.control === 'text'"
										class="jv-action-input"
										v-model="f.value"
										rows="3"
									></textarea>
									<input v-else class="jv-action-input" v-model="f.value" />
								</div>
							</div>
						</div>
						<div
							v-for="(t, ti) in draftPanel.tables"
							:key="t.fieldname"
							class="jv-draft-table"
						>
							<div class="jv-draft-table-title">{{ t.label }}</div>
							<div class="jv-draft-gridwrap">
								<table class="jv-grid">
									<thead>
										<tr>
											<th v-for="c in t.columns" :key="c.fieldname">
												{{ c.label }}
											</th>
											<th class="jv-grid-x"></th>
										</tr>
									</thead>
									<tbody>
										<tr v-for="(r, ri) in t.rows" :key="ri">
											<td
												v-for="c in t.columns"
												:key="c.fieldname"
												:class="{ 'jv-grid-ro': c.read_only }"
											>
												<span v-if="c.read_only">{{
													r[c.fieldname]
												}}</span>
												<template v-else-if="c.fieldtype === 'Link'">
													<input
														class="jv-action-input"
														v-model="r[c.fieldname]"
														@input="
															onDraftLink(
																't:' +
																	ti +
																	':' +
																	ri +
																	':' +
																	c.fieldname,
																() => r[c.fieldname],
																c.options,
																$event
															)
														"
														@focus="
															onDraftLink(
																't:' +
																	ti +
																	':' +
																	ri +
																	':' +
																	c.fieldname,
																() => r[c.fieldname],
																c.options,
																$event
															)
														"
														@blur="closeDraftLink"
														autocomplete="off"
													/>
													<div
														v-if="
															draftLink.open &&
															draftLink.key ===
																't:' +
																	ti +
																	':' +
																	ri +
																	':' +
																	c.fieldname &&
															draftLink.items.length
														"
														class="jv-action-linkmenu"
														:class="{ up: draftLink.up }"
													>
														<button
															v-for="it in draftLink.items"
															:key="it.value"
															@mousedown.prevent="
																pickDraftLink((v) => {
																	r[c.fieldname] = v;
																}, it)
															"
														>
															<b>{{ it.value }}</b
															><span v-if="it.label">
																— {{ it.label }}</span
															>
														</button>
													</div>
												</template>
												<input
													v-else-if="
														[
															'Int',
															'Float',
															'Currency',
															'Percent',
														].includes(c.fieldtype)
													"
													type="number"
													class="jv-action-input"
													v-model="r[c.fieldname]"
												/>
												<input
													v-else-if="c.fieldtype === 'Date'"
													type="date"
													class="jv-action-input"
													v-model="r[c.fieldname]"
												/>
												<select
													v-else-if="c.fieldtype === 'Check'"
													class="jv-action-input jv-action-sel"
													v-model="r[c.fieldname]"
												>
													<option value="1">Yes</option>
													<option value="0">No</option>
												</select>
												<input
													v-else
													class="jv-action-input"
													v-model="r[c.fieldname]"
												/>
											</td>
											<td class="jv-grid-x">
												<button
													class="jv-grid-del"
													@click="removeDraftRow(ti, ri)"
													title="Remove row"
													aria-label="Remove row"
												>
													✕
												</button>
											</td>
										</tr>
									</tbody>
								</table>
							</div>
							<button class="jv-draft-addrow" @click="addDraftRow(ti)">
								＋ Add row
							</button>
						</div>
						<div v-if="draftTotals" class="jv-draft-totals">
							<div class="jv-draft-total-fld">
								<label>Total Qty</label>
								<div class="jv-draft-total-ctl">
									<input
										class="jv-action-input jv-draft-total-input"
										:value="draftTotals.qty"
										readonly
										tabindex="-1"
										aria-readonly="true"
									/>
									<svg
										class="jv-draft-total-lock"
										width="12"
										height="12"
										viewBox="0 0 24 24"
										fill="none"
										stroke="currentColor"
										stroke-width="1.9"
										stroke-linecap="round"
										stroke-linejoin="round"
										aria-hidden="true"
									>
										<rect x="3" y="11" width="18" height="11" rx="2" />
										<path d="M7 11V7a5 5 0 0 1 10 0v4" />
									</svg>
								</div>
							</div>
							<div v-if="draftTotals.hasAmt" class="jv-draft-total-fld">
								<label>Est. Total</label>
								<div class="jv-draft-total-ctl">
									<input
										class="jv-action-input jv-draft-total-input"
										:value="draftTotals.amount"
										readonly
										tabindex="-1"
										aria-readonly="true"
									/>
									<svg
										class="jv-draft-total-lock"
										width="12"
										height="12"
										viewBox="0 0 24 24"
										fill="none"
										stroke="currentColor"
										stroke-width="1.9"
										stroke-linecap="round"
										stroke-linejoin="round"
										aria-hidden="true"
									>
										<rect x="3" y="11" width="18" height="11" rx="2" />
										<path d="M7 11V7a5 5 0 0 1 10 0v4" />
									</svg>
								</div>
							</div>
							<p class="jv-draft-est">
								Auto-calculated · ERPNext computes the final totals.
							</p>
						</div>
						<div v-if="draftPanel.error" style="margin-top: 10px">
							<ActionError :error="draftPanel.error" />
						</div>
					</div>
					<div class="jv-draft-foot">
						<button
							v-if="draftPanel.submittable && draftPanel.verb === 'create'"
							class="jv-action-2nd"
							:disabled="draftPanel.applying"
							@click="applyDraft(1)"
						>
							Create &amp; Submit
						</button>
						<button
							class="jv-action-primary"
							:disabled="draftPanel.applying"
							@click="applyDraft(0)"
						>
							{{ draftPanel.applying ? "Saving…" : draftCta }}
						</button>
						<button class="jv-action-discard" @click="discardDraft">Discard</button>
					</div>
				</aside>
			</div>
		</transition>
		<DraftPreview
			v-if="previewOpen && summaryState.model"
			:model="summaryState.model"
			:headline="(summaryState.view && summaryState.view.headline) || ''"
			:disabled="convStreaming || !!(summaryState.model && summaryState.model.applying)"
			@close="previewOpen = false"
			@edit="onPreviewEdit"
			@confirm="onPreviewConfirm"
		/>

		<ConnectPhoneDialog v-model="showConnect" />
	</div>
</template>

<script setup>
import {
	ref,
	computed,
	inject,
	onMounted,
	onBeforeUnmount,
	onUnmounted,
	nextTick,
	watch,
	watchEffect,
} from "vue";
import { useRoute, useRouter, onBeforeRouteLeave } from "vue-router";
import * as api from "@/api";
import * as voice from "@/api/voice";
import { agentName } from "@/branding";
import { useAudioRecorder } from "@/composables/useAudioRecorder";
import { useDictationRecorder } from "@/composables/useDictationRecorder";
import { createVoiceDictationStore } from "@/utils/voiceDictationStore";
import { isNearSilent } from "@/utils/voiceSilenceGate";
import {
	promoteNewChatScope,
	planRejectedSend,
	createPendingSends,
	injectPendingBubbles,
} from "@/utils/voiceSendGlue";
import {
	createVoiceAudioMirror,
	listOrphanRecordings,
	deleteOrphanRecording,
	LIVE_FRAGMENT_GRACE_MS,
} from "@/utils/voiceAudioMirror";
import { setMacroPrefill } from "@/composables/macroPrefill";
import { takeChatPrefill } from "@/composables/chatPrefill";
import { useConfirm } from "@/composables/useConfirm";
// timezone-safe: naive server datetimes must go through dayjsLocal (site tz)
import { formatDate, exactDate, dayLabel } from "@/utils/datetime";
import { fenceReject, fenceAccept } from "@/utils/eventFence";
import { renderMarkdown } from "@/markdown";
import JvChart from "@/charts/JvChart.vue";
import ConnectPhoneDialog from "@/components/ConnectPhoneDialog.vue";
import JarvisMark from "@/components/JarvisMark.vue";
import DraftPreview from "@/components/doc/DraftPreview.vue";
import ActionError from "@/components/ActionError.vue";
import Banner from "@/components/Banner.vue";
import PendingCard from "@/components/PendingCard.vue";
import ReceiptChip from "@/components/ReceiptChip.vue";
import Message from "@/components/chat/Message.vue";
import Composer from "@/components/chat/Composer.vue";
import AskCard from "@/components/chat/AskCard.vue";
import { parseAsk } from "@/lib/chatAsk";
import { canOpenInDashboards, dashboardOpenRoute } from "@/lib/dashboardOpen";
import { dashboardForConversation } from "@/api/dashboards";
import {
	checkReady,
	readinessDetailOf,
	needsLlmConnection,
	forgetReady,
} from "@/onboarding/readiness.js";
import { suspensionNotice, SUSPENDED_FALLBACK } from "@/onboarding/steps.js";
import { billingBanner } from "@/account/format.js";
import { billingNoticeOf } from "@/onboarding/readiness.js";
import { useShellStore } from "@/stores/shell";
import { useJarvisTheme } from "@/theme";
import { displayName } from "@/lib/user";
import {
	summarize,
	batchFromPreview,
	pendingCardOf,
	verbSentence,
	pendingExpiry,
} from "@/lib/actionSummary";

const session = inject("$session");
const socket = inject("$socket");
const route = useRoute();
const router = useRouter();

// Shared shell state (DESIGN-V3 §4): the sidebar/conversation list lives in
// the app shell now; ChatView is the only writer of currentConvId /
// streamingConvId and the only caller of applyRemote* (socket contract DA-04).
const store = useShellStore();

const currentId = ref(null);
// Remember the open chat per-device so a refresh — or a duplicated tab — restores
// it instead of jumping to whatever sorts first in the sidebar (e.g. a starred
// chat). Also lets a duplicated tab land on the SAME in-progress conversation.
// Also mirrors the selection into the shell store (sidebar active row) — the
// global notifier reads it as "the conversation on screen" on the chat home —
// and clears the sidebar's unread dot for the conversation being opened.
watch(currentId, (id) => {
	store.currentConvId = id;
	if (id) store.clearUnread(id);
	// Phase-0 admission: the queued chip belongs to the conversation we sent
	// from; never carry it into a different chat.
	queuedTurn.value = null;
	confirmedAck.value = false;
	try {
		id
			? localStorage.setItem("jarvis-last-conv", id)
			: localStorage.removeItem("jarvis-last-conv");
	} catch (e) {}
});
const messages = ref([]);
const input = ref("");
// Per-conversation composer drafts: switching chats stashes the leaving
// chat's unsent text here and restores the target chat's own draft, so a
// draft never bleeds into another conversation (and is never lost on an
// accidental switch).
const drafts = ref({});
// Up/Down recall of previously sent prompts (shell-style history).
const promptHistory = ref([]);
const histIdx = ref(null);
const histDraft = ref("");
const sending = ref(false);
const waiting = ref(false);
// Phase-0 admission (chat concurrency): when a send is accepted but QUEUED
// (all in-flight slots taken), the reply hasn't started - we show a "~N ahead"
// chip with a Cancel button instead of the streaming spinner. Cleared when the
// turn is promoted (run:start), cancelled, or errors out.
// { run_id, message_id, position } | null
const queuedTurn = ref(null);
// Transient "Change saved" acknowledgment after a confirm write (SUX-3), shown
// immediately so the card doesn't vanish into silence while the continuation
// turn queues. Auto-clears.
const confirmedAck = ref(false);
let _confirmedAckTimer = null;
const ui = ref({});
// Renew-banner copy when the subscription has lapsed; null while entitled.
// The composer is disabled alongside it (no send can succeed while stopped).
const suspendedNotice = ref(null);
// A DIFFERENT not-ready reason (container_provisioning - e.g. the connected LLM
// account itself ran out of quota, or a container is still coming up) - never a
// billing lapse, so it gets its own quiet copy instead of suspendedNotice's "Chat is
// paused" / Renew framing (2026-07-23 trace: a customer who forced their way past
// onboarding's "Continue to Jarvis" while genuinely not ready used to land here to
// dead silence - the real reason existed the whole time, nobody rendered it).
const notReadyNotice = ref("");
// No AI connected at all (is_ready_for_chat reason "llm_credentials"): the customer
// disconnected their model, or the credential expired. Until now this reason was
// handled by NEITHER the onboarding gate (correctly - see readiness.js) nor any
// banner, so the workspace looked fine and every send failed on arrival. Its own
// flag rather than reusing notReadyNotice: that one carries admin's sentence about a
// container, and this one has its own copy and its own call to action.
const noAiConnected = ref(false);
// Connecting a model is gated on require_jarvis_admin() server-side. A member who
// cannot reach the AI models pane still gets the banner (their chat IS broken and
// they should know why), just without a button that would only bounce them back to
// General.
const canConnectModel = !!(window.is_jarvis_admin || window.is_system_manager);
function goConnectModel() {
	store.openSettings("aimodels");
}
// Billing lifecycle banner. Dismissal is session-only (a ref, not storage): the
// pre-expiry nudge should return on the next visit, since the deadline has not.
const billingNotice = ref({});
const billingDismissedPhase = ref("");
// Renewing is gated on require_jarvis_admin(), which accepts Jarvis Admin OR
// System Manager - match it, or a System Manager who CAN renew is told to ask
// their admin.
const canRenewPlan = !!(window.is_jarvis_admin || window.is_system_manager);
const billingAlert = computed(() => {
	const b = billingBanner(billingNotice.value, canRenewPlan);
	if (!b || billingDismissedPhase.value === b.phase) return null;
	return b;
});
function dismissBillingAlert() {
	billingDismissedPhase.value = (billingAlert.value && billingAlert.value.phase) || "";
}
// Per-conversation "auto-apply changes" (issue #186): seeded from
// get_conversation().conversation.auto_apply on each load; the toggle reflects
// THIS chat. autoApplyNote surfaces the admin-only-enable message.
const convAutoApply = ref(false);
const autoApplyNote = ref("");
// Which builder page started this thread ("dashboards" / "triggers" / ""),
// from get_conversation. A Dashboards build is an ordinary conversation in
// this list; this is the only thing that says its html artifacts are
// dashboards, and it is what gates the "Open in Dashboards" affordance.
const originPage = ref("");
// ...and WHICH conversation that origin was read for. get_conversation answers a
// full round trip after currentId already names the conversation being switched
// to, so the flag alone would still be describing the previous thread while the
// previous thread's cards are on screen. Written together with originPage, only
// when the fetch lands, and compared against currentId at the gate: a switch (or
// a fetch that never lands) fails closed, while a refresh of the conversation
// already open — message:enriched, the resync on every tab focus — invalidates
// nothing.
const originOf = ref("");
// The <Composer> instance. It owns the textarea and auto-grow; this view still
// owns everything around them, so it reaches in for the two things a parent
// legitimately needs: `composerRef.value.el` (the raw textarea, for the caret
// math behind mention insertion and edit-and-resend) and `focusInput()`.
const composerRef = ref(null);
const threadEl = ref(null);
const threadInnerEl = ref(null);
const rootEl = ref(null);
// Jump-to-latest arrow: shown when the thread is scrolled up away from the newest
// message. `pinnedToBottom` tracks whether we should auto-stick to the bottom as
// content grows (streaming replies, late-loading images/charts) — true until the
// user deliberately scrolls up.
const showScrollDown = ref(false);
const pinnedToBottom = ref(true);

// ---- Reusable notifier -----------------------------------------------------
// notify("Deleted", { type: "success" }) drops a lightweight toast that stacks
// bottom-right and auto-dismisses. (The confirm dialog it used to sit beside now
// lives app-wide in composables/useConfirm.js + the shell's ConfirmDialog — call
// confirm() from anywhere.)
const notes = ref([]);
let _noteSeq = 0;
function notify(message, opts = {}) {
	const id = ++_noteSeq;
	notes.value = [
		...notes.value,
		{ id, message, type: opts.type || "info", title: opts.title || "" },
	];
	const ttl = opts.duration == null ? 3200 : opts.duration;
	if (ttl > 0) setTimeout(() => dismissNote(id), ttl);
	return id;
}
function dismissNote(id) {
	notes.value = notes.value.filter((n) => n.id !== id);
}
// Announce to screen readers via the visually-hidden live region. Clears first
// so an identical repeated message (e.g. two "Jarvis replied." in a row) still
// re-announces.
function announceSR(msg) {
	srMessage.value = "";
	nextTick(() => {
		srMessage.value = msg;
	});
}
// Confirm dialog now lives app-wide (composables/useConfirm.js + the single
// <ConfirmDialog> in AppShell) — this view just calls confirm(). Escape/backdrop
// dismissal and rendering are owned by that component.
const { confirm } = useConfirm();
const modelMenuOpen = ref(false);
const showConnect = ref(false);
// One-shot "ground on wiki": when armed, the NEXT message carries a
// context.ground_wiki flag so the backend injects relevant wiki page bodies
// into that turn. Cleared after each send (see send()).
const groundNextTurn = ref(false);
// (sidebar collapse machinery, per-conversation ⋯ menu and inline rename
// moved to the app shell — stores/shell.js + components/shell/*, §3.7)
const modelOverride = ref("");
// Reasoning effort for this conversation. "" = inherit Jarvis Settings.
const thinkingOverride = ref("");

// ---- recurring business-note greeting banner ----
// Fires on every third genuinely-new chat (server-counted) while the user has
// no Business voice note and hasn't dismissed it. No conversation of its own,
// no snooze state: the cadence itself IS the "maybe later" - the card just
// comes back on the next multiple-of-three chat. The server owns eligibility;
// this only tracks what to draw for the current mount.
const bizGreeting = ref({ show: false });

// Probe on mount and again after every genuinely-new chat. Never blocks chat:
// a failure just means no card.
async function probeGreeting() {
	try {
		const res = await api.maybeGreet();
		bizGreeting.value = { show: !!res?.show_card };
	} catch (e) {
		/* greeting is never load-bearing */
	}
}

function greetingShowMeWhere() {
	router.push({ path: "/skills", hash: "#business" });
}

// "Maybe later" hides instantly, then records the current cadence tick on the
// server so a refresh can't re-show a card the user just closed. Fire-and-
// forget: if the call fails the worst case is the old behaviour (card back on
// refresh), never a blocked click.
function greetingLater() {
	bizGreeting.value = { ...bizGreeting.value, show: false };
	api.hideGreeting().catch(() => {});
}

async function greetingNeverAsk() {
	bizGreeting.value = { ...bizGreeting.value, show: false };
	try {
		await api.dismissGreeting();
	} catch (e) {
		/* ignore */
	}
}

// ---- settings panel (openclaw-style console) ----
// The overlay is bound to the SHELL's settingsOpen (D9): the user menu opens
// it from any route via store.openSettings(router); Esc/close write through.
const settingsOpen = computed({
	get: () => store.settingsOpen,
	set: (v) => {
		store.settingsOpen = v;
	},
});
const settingsTab = ref("overview");
// AI models is the ONLY pane that can change the chat-readiness verdict: every
// connect, rotate and disconnect goes through LlmPoolEditor, which only that pane
// renders (SettingsDialog.vue PANES). Closing the dialog is one of the most common
// actions in the app, so remember whether that pane was actually opened during
// this visit rather than paying for a readiness round-trip every time someone
// glances at the theme and closes. Tracked across the whole visit, not read at
// close time, because the customer can disconnect and then navigate to another
// pane before closing.
let aiModelsPaneVisited = false;
const SETTINGS_READINESS_SECTION = "aimodels";
watch(
	() => store.settingsSection,
	(section) => {
		if (store.settingsOpen && section === SETTINGS_READINESS_SECTION) {
			aiModelsPaneVisited = true;
		}
	}
);
// Load usage stats whenever the dialog opens (was openSettings()).
watch(
	() => store.settingsOpen,
	async (open) => {
		if (!open) {
			// The AI models pane lives in this dialog, so connecting or disconnecting a
			// model happens while chat is still mounted behind it. The readiness verdict
			// is memoized for the page load, so without dropping it here the composer
			// would stay dead after a reconnect, or stay live after a disconnect until
			// the customer reloaded. Best-effort, exactly like the boot read.
			if (!aiModelsPaneVisited) return;
			aiModelsPaneVisited = false;
			forgetReady();
			try {
				noAiConnected.value = await needsLlmConnection();
			} catch (e) {
				/* leave the last known state */
			}
			return;
		}
		// openSettings(section) writes both refs in the same tick, so the section
		// watcher above may not fire for a dialog opened DIRECTLY on AI models (the
		// "Connect a model" banner button does exactly that, and the section is
		// sticky, so it can already hold "aimodels" from a previous visit). Seed the
		// flag from the section the dialog is opening on instead of relying on a
		// change event that may never come.
		aiModelsPaneVisited = store.settingsSection === SETTINGS_READINESS_SECTION;
		try {
			usage.value = await api.getUsage(currentId.value);
		} catch (e) {
			/* usage stays null → the section shows a dash */
		}
	}
);

// --- Custom skills (Skills settings tab + "/" composer menu) ---
const customSkills = ref([]);

function _skillErr(e) {
	return (e && ((e.messages && e.messages[0]) || e.message)) || "Something went wrong.";
}
async function loadCustomSkills() {
	try {
		customSkills.value = (await api.listCustomSkills()) || [];
	} catch (e) {
		/* keep prior */
	}
}

// --- Macros (customer-authored prompt sequences run as chained turns) ---
const macros = ref([]);
// (approvals badge → store.refreshApprovalsCount; legacy #hash deep-links →
// router-level redirects, §9)

// The live macro-run banner: { run, conversation, step, total, label, status }.
const macroRun = ref(null);
let _macroDoneTimer = null;

async function loadMacros() {
	try {
		macros.value = (await api.listMacros()) || [];
	} catch (e) {
		/* keep prior */
	}
}

async function stopMacro() {
	if (!macroRun.value) return;
	try {
		await api.stopMacroRun(macroRun.value.run);
	} catch (e) {
		/* ignore */
	}
}

// ---- Macro run history dashboard (settings → Macro runs) ----
const MACRO_RUN_PAGE = 30;
const macroRuns = ref([]);
const macroRunStats = ref(null);
const macroRunStatus = ref(""); // "" = all
const macroRunMacro = ref(""); // "" = all macros
const macroRunStart = ref(0);
const macroRunHasMore = ref(false);
const macroRunsLoading = ref(false);
const MACRO_RUN_STATUSES = ["", "running", "completed", "failed", "stopped"];

async function loadMacroRunStats() {
	try {
		macroRunStats.value = await api.macroRunStats();
	} catch (e) {
		/* keep prior */
	}
}
// reset=true starts a fresh page-1 load (also refreshes stats + the macro
// filter options); reset=false appends the next page ("Load more").
async function loadMacroRuns(reset = true) {
	if (macroRunsLoading.value) return;
	macroRunsLoading.value = true;
	if (reset) {
		macroRunStart.value = 0;
		loadMacroRunStats();
		if (!macros.value.length) loadMacros(); // populate the macro filter dropdown
	}
	try {
		const r = await api.listMacroRuns({
			status: macroRunStatus.value,
			macro: macroRunMacro.value,
			limit: MACRO_RUN_PAGE,
			start: macroRunStart.value,
		});
		const rows = (r && r.runs) || [];
		macroRuns.value = reset ? rows : [...macroRuns.value, ...rows];
		macroRunHasMore.value = !!(r && r.has_more);
		macroRunStart.value += rows.length;
	} catch (e) {
		/* keep the last-good list */
	} finally {
		macroRunsLoading.value = false;
	}
}
function setMacroRunStatus(s) {
	macroRunStatus.value = s;
	loadMacroRuns(true);
}
function setMacroRunMacro(e) {
	macroRunMacro.value = e.target.value;
	loadMacroRuns(true);
}

// Row actions -------------------------------------------------------------
async function openRunConversation(run) {
	if (!run.conversation) return;
	settingsOpen.value = false;
	await store.loadConversations();
	await selectConversation(run.conversation);
}
async function rerunFromHistory(run) {
	try {
		const res = await api.runMacro(run.macro);
		const data = (res && res.data) || res || {};
		settingsOpen.value = false;
		await store.loadConversations();
		if (data.conversation) await selectConversation(data.conversation);
		macroRun.value = {
			run: data.macro_run,
			conversation: data.conversation,
			step: 0,
			total: 0,
			label: "",
			status: "running",
		};
	} catch (e) {
		notify(_skillErr(e), { type: "error" });
	}
}
async function stopRunFromHistory(run) {
	try {
		await api.stopMacroRun(run.name);
		run.status = "stopped"; // optimistic patch
		loadMacroRunStats();
	} catch (e) {
		notify(_skillErr(e), { type: "error" });
	}
}

// Formatters --------------------------------------------------------------
function macroRunBadge(status) {
	return (
		{ completed: "ok", failed: "err", running: "run", queued: "run", stopped: "stop" }[
			status
		] || "stop"
	);
}
function fmtAgo(dt) {
	if (!dt) return "";
	const t = new Date(String(dt).replace(" ", "T")).getTime();
	if (isNaN(t)) return "";
	const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
	if (s < 60) return "just now";
	const m = Math.floor(s / 60);
	if (m < 60) return `${m}m ago`;
	const h = Math.floor(m / 60);
	if (h < 24) return `${h}h ago`;
	const d = Math.floor(h / 24);
	if (d < 7) return `${d}d ago`;
	return new Date(t).toLocaleDateString();
}
function fmtDuration(sec) {
	if (sec == null) return "";
	sec = Math.max(0, Math.round(sec));
	if (sec < 60) return `${sec}s`;
	const m = Math.floor(sec / 60),
		s = sec % 60;
	if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
	const h = Math.floor(m / 60);
	return `${h}h ${m % 60}m`;
}
// Elapsed for a run that hasn't finished (running/queued) — shows "· 18s".
function macroRunElapsed(run) {
	if (run.duration_s != null || !run.started_at) return fmtDuration(run.duration_s);
	const t = new Date(String(run.started_at).replace(" ", "T")).getTime();
	if (isNaN(t)) return "";
	return fmtDuration((Date.now() - t) / 1000) + " elapsed";
}
// Live-patch an open dashboard from macro:progress / macro:done events.
function patchMacroRunRow(p, done) {
	if (!settingsOpen.value || settingsTab.value !== "macroruns") return;
	const row = macroRuns.value.find((r) => r.name === p.macro_run);
	if (row) {
		if (p.step != null) row.current_step = p.step;
		row.status = done ? p.status || "completed" : "running";
	}
	loadMacroRunStats();
}
// Load the dashboard when the user enters the Macro runs tab (fresh each time).
watch(
	() => settingsOpen.value && settingsTab.value === "macroruns",
	(active) => {
		if (active) loadMacroRuns(true);
	}
);
const usage = ref(null); // { estimated, chat_tokens, month_tokens, total_tokens, budget_monthly, month_label }
// Compact token count: 1234 → "1.2k", 2_500_000 → "2.5M".
function fmtTokens(n) {
	n = Number(n || 0);
	if (n >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
	if (n >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
	return String(n);
}
const usagePct = computed(() => {
	const u = usage.value;
	if (!u || !u.budget_monthly) return 0;
	return Math.min(100, Math.round((u.month_tokens / u.budget_monthly) * 100));
});
// theme: the shared composable (module singleton, src/theme.js) replaces the
// old private palette copy (D34). It also bridges `data-theme` onto <html>
// for frappe-ui; the .jv-dark class + inline paletteVars below keep the jv-*
// scoped styles untouched.
const { theme, effectiveDark, paletteVars, setTheme, toggleTheme } = useJarvisTheme();
// Flip "confirm before changes" for THIS conversation (issue #186). Optimistic;
// reverts on failure. auto_apply=1 = skip confirmation (auto mode). Enabling is
// admin-only server-side - a non-admin gets a 403, so we revert + show a note.
async function toggleAutoApply() {
	if (!currentId.value) return;
	const next = convAutoApply.value ? 0 : 1;
	autoApplyNote.value = "";
	convAutoApply.value = !!next; // optimistic
	try {
		const r = await api.setAutoApply(currentId.value, next);
		// Response envelope is {ok, data:{auto_apply}} - trust the server's value.
		if (r && r.data && typeof r.data.auto_apply !== "undefined")
			convAutoApply.value = !!r.data.auto_apply;
	} catch (e) {
		convAutoApply.value = !next; // revert
		// Enabling requires System Manager; a non-admin gets a PermissionError (403).
		if (next) autoApplyNote.value = "Only an administrator can enable auto-apply.";
	}
}

// Phase 1: streaming/metrics, live tool activity, file input, mentions, stop
const runStartMs = ref(0);
const nowMs = ref(0); // ticks every 1s while busy → drives the live elapsed timer
const currentRunId = ref(null);
const stoppedRunId = ref(null);
const stoppedMsgIds = ref(new Set()); // assistant rows the user stopped — ignore later (incl. "recovered") events for them
const currentMsgId = ref(null); // in-flight assistant row id (from run:start) — lets Stop pin the reply even before the first token
const errorMeta = ref({}); // { [message_id]: { code, changed_data } } from a live run:error (not persisted; a refresh falls back to classifying the error string)
// Pump streaming (Relay Pump) end-to-end epoch/seq fence (CDX-3 + CDX-12). The pure fence
// logic lives in @/utils/eventFence.js (extracted so it is unit-tested by a real node test
// — jarvis/tests/test_event_fence_client.py runs frontend/src/utils/eventFence.test.js).
// The fence is RUN-SCOPED — ONE entry per run_id — and is applied to EVERY pump event type
// (deltas, run:start/recovering/error/end, and ALL tool events incl. jarvis__*). CDX-12:
// the FIRST terminal at the current epoch is accepted on seq equality (it shares the delta
// watermark's seq), and a REPEAT terminal is then rejected one-shot; without this the
// normal terminal was bounced and the run:end UI cleanup never ran (stuck spinner / Stop
// state / duplicate activity block — F3). Legacy events (no pump_epoch) bypass entirely.
const eventFence = ref({}); // { [run_id]: { epoch, seq, terminated } }
function pumpFenceReject(p, isTerminal) {
	return fenceReject(eventFence.value, p, isTerminal);
}
function pumpFenceAccept(p, isTerminal) {
	fenceAccept(eventFence.value, p, isTerminal);
}
// F3 (defensive resync parity): tear down the live streaming-activity block (jv-mark +
// tool tally) + composer/streaming state for a run whose assistant reply is settled, even
// if run:end was never processed (a missed/late terminal). The run:end handler already
// does this on the happy path; this guard makes a settled message ALWAYS collapse the
// orphan activity container so the live DOM matches what a reload renders.
function clearStreamingActivity() {
	waiting.value = false;
	sending.value = false;
	statusPhase.value = null;
	activeTools.value = [];
	currentRunId.value = null;
	store.streamingConvId = null;
	recovering.value = null;
}
function tearDownActivityIfSettled() {
	const rid = currentRunId.value;
	if (!rid) return;
	const f = eventFence.value[rid];
	const settledByFence = f && f.terminated != null; // a terminal was accepted for this run
	const mid = currentMsgId.value;
	const m = mid ? messages.value.find((x) => x.name === mid) : null;
	const settledByRow = m && m.streaming === false; // the assistant row is no longer streaming
	if (settledByFence || settledByRow) clearStreamingActivity();
}
// SUX-7: a settled reply whose enrichment (canvas/attachments/title) is still running.
// run:end carries enrichment_pending; message:enriched clears it. Drives a subtle
// "finishing…" affordance so a late pop-in is signalled, not silent.
const enrichmentPending = ref(new Set()); // message_ids awaiting message:enriched
const recovering = ref(null); // { message_id, reason } while a turn is parked for background recovery — the composer stays UNLOCKED so the user isn't trapped
const retrying = ref(false); // guards the error-card Retry against a double-enqueue while one is in flight
const srMessage = ref(""); // visually-hidden aria-live text (turn completion / failure) for screen readers
const activeTools = ref([]); // [{ id, name, status }] for the in-flight run
// Live activity shows ONE tool at a time: the most-recently-started tool that's
// still running, plus a compact count of the ones already finished this turn.
const currentTool = computed(
	() => [...activeTools.value].reverse().find((t) => t.status === "running") || null
);
const doneCount = computed(() => activeTools.value.filter((t) => t.status !== "running").length);
const failedCount = computed(() => activeTools.value.filter((t) => t.status === "error").length);
// ── Live status line ────────────────────────────────────────────────────────
// Real progress instead of a blanket "Thinking…": phase transitions come from
// the run's realtime events (run:start → tool:start/end → assistant:delta).
// Faithful by construction: tool phrases derive from the tool NAME plus
// openclaw's own arg summary (tool_title, e.g. "get_list Sales Invoice") —
// nothing is invented client-side.
const statusPhase = ref(null); // 'model' | 'analyzing' | null
const TOOL_PHRASES = {
	get_list: "Fetching {d} records",
	get_doc: "Opening {d}",
	get_schema: "Checking the {d} structure",
	run_report: "Running the {d} report",
	get_report_filters: "Checking report filters",
	query: "Querying the database",
	summarize_dataset: "Summarizing the data",
	get_stock_balance: "Checking stock balance",
	get_balance_on: "Checking account balance",
	get_customer_outstanding: "Checking outstanding",
	add_tag: "Tagging {d}",
	remove_tag: "Untagging {d}",
	assign_to: "Assigning {d}",
	share_doc: "Sharing {d}",
	add_comment: "Adding a comment",
	send_email: "Sending the email",
	create_doc: "Drafting a {d}",
	update_doc: "Updating {d}",
	submit_doc: "Submitting {d}",
	cancel_doc: "Cancelling {d}",
	delete_doc: "Deleting {d}",
	amend_doc: "Amending {d}",
	preview_doc: "Previewing the draft",
	export_excel: "Preparing your spreadsheet",
	download_pdf: "Preparing your PDF",
	download_vcard: "Preparing the contact card",
	attach_to_doc: "Attaching the file",
	read_file: "Reading the file",
	get_file_pages: "Reading the document",
	run_method: "Running the operation",
	bash: "Reading reference material",
	exec: "Reading reference material",
	browser: "Browsing the web",
	canvas: "Drawing the canvas",
	image: "Generating the image",
};
function toolPhrase(tool) {
	if (!tool) return "";
	const raw = String(tool.name || "");
	const base = raw.replace(/^jarvis__/, "");
	// openclaw's title is "<toolName> <arg summary>"; the remainder after the
	// tool name is its own faithful description of the target.
	let detail = "";
	if (tool.title) {
		const title = String(tool.title);
		detail = (
			title.startsWith(raw)
				? title.slice(raw.length)
				: title.startsWith(base)
				? title.slice(base.length)
				: ""
		).trim();
	}
	if (detail.length > 60) detail = detail.slice(0, 57) + "…";
	const tpl = TOOL_PHRASES[base];
	if (!tpl) return detail ? `Using ${base} (${detail})…` : `Using ${base}…`;
	if (tpl.includes("{d}")) {
		return (
			(detail
				? tpl.replace("{d}", detail)
				: tpl.replace(/ ?\{d\}/, "").replace("the  report", "the report")) + "…"
		);
	}
	return tpl + "…";
}
const liveStatus = computed(() => {
	if (statusPhase.value === "waking") return "Waking up your assistant…";
	if (currentTool.value) return toolPhrase(currentTool.value);
	if (statusPhase.value === "analyzing") return "Analyzing the results…";
	if (waiting.value || sending.value || statusPhase.value === "model") return "Working on it…";
	return thinkingWord.value;
});
// Recovery banner copy: compaction (context overflow, retrying) vs a connection
// hiccup. Both mean "still working, in the background" — not an error.
// Renew CTA → Settings ▸ Plan & billing, the only surface that takes payment.
function goRenew() {
	// Straight to our renewal flow (the Desk billing page's Razorpay checkout),
	// not the settings pane the customer would then have to click through. The
	// Renew button only shows for someone who can actually renew.
	window.location.assign("/app/jarvis-account?billing=1");
}
const recoveringLabel = computed(() =>
	recovering.value && recovering.value.reason === "compacting"
		? "That was a big one — reorganizing the conversation and retrying…"
		: "Reconnecting — your answer will appear here when it's ready."
);
// Failure taxonomy → a plain-language headline. The raw string still shows
// behind "Show details". `code` comes from the live run:error event; a refresh
// (which only has the persisted string) classifies it here.
const ERROR_HEADLINES = {
	unreachable: "I couldn't reach the assistant",
	timeout: "That took too long",
	provider: "The model is busy right now",
	"recovery-expired": "This took too long, so I stopped waiting",
	internal: "Something went wrong",
	cancelled: "This message was cancelled",
};
// Phase-0 admission (chat concurrency): the ONLY place a Turn's internal state
// name maps to user-facing copy (SUX-8). No raw internal state name
// ("dispatching", "queued", …) ever renders in the UI. `queued` takes a
// position and reads "~N ahead" (approximate, SUX-2).
// Phase-0 WIRES `queued` (the chip) and `cancelled` (cancelQueued toast + the
// turn:cancelled fallback). `dispatching`/`errored`/`done` are defined for the
// full state set but are NOT reached in Phase 0 (run:start jumps straight to the
// pre-existing "Thinking…" UI; reply errors flow through ERROR_HEADLINES; a done
// reply renders normally) — kept so the mapping is complete for WP-1 (SUXI-7).
const TURN_STATE_COPY = {
	queued: (pos) => (pos && pos > 0 ? `Queued — ~${pos} ahead` : "Queued"),
	// SUXF-3: the pump introduces a queued->preparing->ready window (prompt assembly
	// + session bootstrap) between "queued" and the stream. Give it copy so the chip
	// reads "Starting…" instead of freezing on a stale "~N ahead" / going silent.
	preparing: () => "Starting…",
	ready: () => "Starting…",
	dispatching: () => "Starting…",
	cancelled: () => "Cancelled",
	errored: () => "Something went wrong",
	done: () => "",
};
function queuedChipLabel(pos, state) {
	// SUXF-3: once a turn leaves `queued` (preparing/ready/dispatching) the position
	// is meaningless — show "Starting…" so the chip never contradicts what's actually
	// happening while the message is being prepared.
	if (state && TURN_STATE_COPY[state] && state !== "queued") return TURN_STATE_COPY[state]();
	return TURN_STATE_COPY.queued(pos);
}
function classifyErrorCode(raw) {
	const low = (raw || "").toLowerCase();
	// Phase-0 admission cancel markers (SUXI-4): a queued turn cancelled by the
	// user or aged out by the system leaves a durable transcript marker so a
	// later reload shows WHY there's no reply (not a silent drop). Classified as
	// "cancelled" so it renders as a muted note, NOT a red "something went wrong".
	if (
		low.startsWith("you cancelled this message") ||
		low.startsWith("waited too long in the queue")
	)
		return "cancelled";
	if (
		low.includes("ws open failed") ||
		low.includes("unreachable") ||
		low.includes("connection timed out")
	)
		return "unreachable";
	if (low.includes("recovery window")) return "recovery-expired";
	if (low.includes("timed out") || low.includes("timeout") || low.includes("deadline"))
		return "timeout";
	if (
		[
			"quota",
			"rate limit",
			"cooldown",
			"overloaded",
			"insufficient",
			"credit",
			"billing",
		].some((k) => low.includes(k))
	)
		return "provider";
	return "internal";
}
function errorInfo(m) {
	const meta = errorMeta.value[m.name] || {};
	const code = meta.code || classifyErrorCode(m.error);
	return {
		code,
		headline: ERROR_HEADLINES[code] || "Something went wrong",
		noChange: meta.changed_data === false,
	};
}
// Live elapsed timer shown next to the status line so a long turn reads as
// "still working" (time ticking) rather than a frozen spinner. Hidden for the
// first few seconds so quick turns don't flash a "0s".
const liveElapsedLabel = computed(() => {
	if (!busy.value || !runStartMs.value || !nowMs.value) return "";
	const s = (nowMs.value - runStartMs.value) / 1000;
	return s >= 3 ? fmtDuration(s) : "";
});
const runMeta = ref({}); // { [message_id]: { ms, tools, names } } — survives reloads
const canvasContent = ref({}); // { `${msgName}::${canvasName}`: srcdoc html (html/svg) | data-url (pdf/image/file) }
const pendingFiles = ref([]); // [{ file_url, file_name }] attachments to send
const uploading = ref(false);
const mention = ref({ open: false, type: "", query: "", start: 0, items: [], index: 0 });
// Tool names for the "Tools available" count + the /tool autocomplete. Seeded
// with the core set as a fallback, then replaced on mount with the live bench
// registry (jarvis.chat.api.list_tools) so it reflects every registered tool
// instead of drifting from a hardcoded list.
const jarvisTools = ref([
	"get_list",
	"get_doc",
	"get_schema",
	"query",
	"run_report",
	"create_doc",
	"update_doc",
	"submit_doc",
	"cancel_doc",
	"amend_doc",
	"delete_doc",
]);

// Shared with the shell via @/lib/user (see the cookie double-decode note there).
const fullName = displayName(session.user);
const firstName = computed(() => fullName.split(/\s+/)[0]);
const greeting = computed(() => {
	const h = new Date().getHours();
	return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
});
// Empty override = "Auto": the backend falls back to Jarvis Settings.llm_model.
const modelLabel = computed(() => modelOverride.value || "Auto");
const availableModels = computed(
	() => ui.value.subscription_models?.[ui.value.llm_provider] || []
);

// Effort levels the SERVER accepts. Never hard-code these: Jarvis Conversation
// .thinking_override is a Select limited to low/medium/high, and offering a
// level it rejects (openclaw itself also knows xhigh/max) would fail the save.
const thinkingLevels = computed(() => ui.value.thinking_levels || ["low", "medium", "high"]);
const thinkingLabel = computed(() => thinkingOverride.value || "auto");

// The pickable models: the configured LLM pool when present, else the
// provider's subscription allowlist. Deduped on provider+model because a
// subscription pool legitimately holds one row per ACCOUNT, not per model.
const pickableModels = computed(() => {
	const pool = ui.value.pool_models || [];
	if (pool.length) {
		const seen = new Set();
		const out = [];
		for (const r of pool) {
			const key = `${r.provider}/${r.model}`;
			if (seen.has(key)) continue;
			seen.add(key);
			out.push(r);
		}
		return out;
	}
	return availableModels.value.map((m) => ({
		provider: ui.value.llm_provider || "",
		model: m,
		tier: "",
	}));
});

// Show the provider grouping only when the customer actually has a choice.
// A subscription pool stores provider="" on every row, so without this the
// menu would render a "Provider" header above a single blank group.
const showProviders = computed(() => !!ui.value.multi_provider);
const modelsByProvider = computed(() => {
	const groups = new Map();
	for (const r of pickableModels.value) {
		const p = r.provider || ui.value.llm_provider || "default";
		if (!groups.has(p)) groups.set(p, []);
		groups.get(p).push(r);
	}
	return [...groups.entries()].map(([provider, models]) => ({ provider, models }));
});

const currentTitle = computed(
	() => store.conversations.find((c) => c.name === currentId.value)?.title || "New chat"
);

// Same dual kill-switch the support routes guard on (router/index.js
// supportGuard) — a dead button that redirects straight back to chat is worse
// than no button.
const supportOn = window.support_available && window.has_support_access;

// "Get help from a human" — always available (v1). The conversation reference
// is plain, readable, user-EDITABLE body text: create_ticket takes only
// (subject, body), has no context params, and derives the tenant server-side
// from the API key. An operator-actionable deep-link is v2.
function getHumanHelp() {
	const title = (currentTitle.value || "").trim();
	// "New chat" is currentTitle's fallback, not a real title — carrying it
	// would put a meaningless quote in front of a support agent.
	const ref = title && title !== "New chat" ? `\n\n- From Jarvis chat: "${title}"` : "";
	router.push({ name: "SupportNew", query: ref ? { body: ref } : {} });
}

// A FAILED tool call with no assistant turn ahead of it in the thread, keyed by
// message name → the copy to render. The Activity accordion is anchored to an
// assistant message, so a tool row that arrives before any assistant text has
// nowhere to hang and renders NOWHERE — which is exactly the shape an autonomous
// delegate produces when its very first call is refused. Those rows are surfaced
// inline instead; everything else keeps the accordion behaviour untouched. The
// server writes plain, customer-facing text into error.message / error.hint for
// this surface, so both are shown as-is.
const orphanToolFailures = computed(() => {
	const out = {};
	let cur = null;
	for (const m of messages.value) {
		if (m.role === "user") cur = null;
		else if (m.role === "assistant") cur = m.name;
		else if (m.role === "tool" && !cur && !m.action_outcome && m.tool_status === "error") {
			let v = m.tool_result;
			if (typeof v === "string") {
				try {
					v = JSON.parse(v);
				} catch (e) {
					v = null;
				}
			}
			const err = (v && v.error) || {};
			out[m.name] = {
				message:
					(typeof err.message === "string" && err.message.trim()) ||
					"This step couldn't be completed.",
				hint: (typeof err.hint === "string" && err.hint.trim()) || "",
			};
		}
	}
	return out;
});
const visibleMessages = computed(() =>
	messages.value.filter((m) => {
		// Receipt-chip rows (a confirmed / discarded / failed gated write) render
		// inline in the thread; every OTHER role=tool row belongs to the collapsed
		// Activity accordion, not the main thread — except an orphaned FAILURE,
		// which the accordion cannot hold.
		if (m.role === "tool") return !!m.action_outcome || !!orphanToolFailures.value[m.name];
		if (m.role !== "user" && m.role !== "assistant") return false;
		// Hide a blank streaming placeholder. The live "Working on it…" indicator
		// below the thread already renders the assistant logo + status for the
		// in-flight turn; after a refresh or tab-switch the server's still-empty
		// streaming row loads alongside it, so drawing both showed the assistant
		// logo twice (once blank, once beside the status). A placeholder that has
		// text, an error, or a canvas is a real reply and always renders.
		if (
			m.role === "assistant" &&
			m.streaming &&
			!(m.content || "").trim() &&
			!m.error &&
			!(m.canvas && m.canvas.length)
		)
			return false;
		return true;
	})
);
// Group role=tool messages under the assistant turn they belong to, so each
// answer can show an expandable "Activity" list of the tool calls (with input
// + output) that produced it — openclaw-style. Tool rows follow their
// assistant placeholder in seq order, so we attach to the most recent
// assistant message and reset on each user message.
const activityByAssistant = computed(() => {
	const map = {};
	let cur = null;
	for (const m of messages.value) {
		if (m.role === "user") cur = null;
		else if (m.role === "assistant") {
			cur = m.name;
			if (!map[cur]) map[cur] = [];
		}
		// action_outcome rows are receipt chips shown inline, not accordion tool calls.
		else if (m.role === "tool" && cur && !m.action_outcome)
			(map[cur] || (map[cur] = [])).push(m);
	}
	return map;
});
const activityOpen = ref({});
const toolOpen = ref({});
// Whether replies reveal which tools + skills produced them. When off, the
// chat shows only a generic "Thinking…/Working…" indicator and hides the
// per-reply tool/skill chips. Persisted per device.
// These device prefs now live in the shell store (owned there so the hoisted
// settings dialog's GeneralPane toggle and this view's live gating stay in sync
// same-tab — a pane-local ref could not notify this view). This view only READS
// them (to gate the tool-activity rows); the toggles are driven from GeneralPane
// via store.setActivityDetail / store.toggleNotify. The actual notification
// dispatch on reply-ready no longer lives here — it moved to the app-scoped
// notifier (notify/globalNotifier.js, attached by AppShell) so it also fires
// for background conversations and non-chat routes.
const showActivityDetail = computed(() => store.activityDetail);
const notifyEnabled = computed(() => store.notifyEnabled);

// Danger zone: wipe every conversation + message (macros/skills untouched).
const clearingHistory = ref(false);
async function clearAllHistory() {
	if (
		!(await confirm({
			title: "Delete ALL chat history?",
			message:
				"Every conversation and message will be permanently deleted. Macros, skills and settings stay. This can't be undone.",
			confirmLabel: "Delete everything",
			danger: true,
		}))
	)
		return;
	clearingHistory.value = true;
	try {
		await api.clearChatHistory();
		messages.value = [];
		originPage.value = "";
		originOf.value = "";
		currentId.value = "";
		settingsOpen.value = false;
		newChat(); // also reloads store.conversations
	} catch (e) {
		notify(_skillErr(e) || "Could not delete history", { type: "error" });
	} finally {
		clearingHistory.value = false;
	}
}
// In-flight wording shown while a turn runs (tool-activity hidden). Kept to a
// single neutral "Thinking\u2026" \u2014 no task-describing phrases that could overclaim.
const THINK_WORDS = ["Thinking\u2026"];
const thinkTick = ref(0);
let _thinkTimer = null;
const thinkingWord = computed(() => THINK_WORDS[thinkTick.value % THINK_WORDS.length]);
// Persisted per-reply tool count + duration so they survive a refresh (runMeta
// is live-session only): count from the saved tool messages, duration on ONE
// baseline shared with the live timer (CDX-20). The live value (runMeta.ms) is the
// client-side run:start -> run:end span; the persisted value (reply_duration_ms,
// stamped at settlement) is the server-side dispatching_at(run:start) -> settlement
// span — the SAME boundary, so a reloaded reply matches its live reading within
// network tolerance. reply_duration_ms is NULL on legacy (non-pump) rows, which fall
// back to the modified-creation span (creation is NO LONGER rewritten as a metric).
function toolCountOf(m) {
	return (activityByAssistant.value[m.name] || []).length;
}
function elapsedOf(m) {
	const live = runMeta.value[m.name] && runMeta.value[m.name].ms;
	if (live) return (live / 1000).toFixed(1);
	if (m.reply_duration_ms != null && m.reply_duration_ms !== "") {
		const d = Number(m.reply_duration_ms) / 1000;
		if (d >= 0 && d < 1800) return d.toFixed(1);
	}
	// Legacy fallback: rows with no persisted duration (pre-pump / legacy transport)
	// use the assistant row's modified-minus-creation span.
	if (m.creation && m.modified) {
		const d =
			(new Date(m.modified.replace(" ", "T")) - new Date(m.creation.replace(" ", "T"))) /
			1000;
		if (d >= 0 && d < 1800) return d.toFixed(1);
	}
	return "";
}
// Response-duration label: keeps the sub-minute look (e.g. "12.4s") but rolls
// over to minutes once it passes 60s (e.g. "1m 5s" / "2m").
function elapsedLabel(m) {
	const raw = elapsedOf(m);
	if (!raw) return "";
	const sec = parseFloat(raw);
	if (sec < 60) return `${raw}s`;
	const total = Math.round(sec);
	const mm = Math.floor(total / 60),
		ss = total % 60;
	return ss ? `${mm}m ${ss}s` : `${mm}m`;
}
// Open state falls back to the pref until the user explicitly toggles a turn.
function isActivityOpen(name) {
	return name in activityOpen.value ? activityOpen.value[name] : false;
}
function toggleActivity(name) {
	activityOpen.value = { ...activityOpen.value, [name]: !isActivityOpen(name) };
}
function toggleTool(name) {
	toolOpen.value = { ...toolOpen.value, [name]: !toolOpen.value[name] };
}
function toolLabel(n) {
	return (n || "tool").replace(/^jarvis__/, "");
}
function activityNames(assistantName) {
	return (activityByAssistant.value[assistantName] || [])
		.map((t) => toolLabel(t.tool_name))
		.join(", ");
}
// args/result are stored as JSON strings — pretty-print, and trim very large
// payloads so a 10k-row result doesn't blow up the chat.
function prettyJson(s) {
	if (s == null || s === "") return "";
	let v = s;
	try {
		v = typeof s === "string" ? JSON.parse(s) : s;
	} catch (e) {
		return String(s).slice(0, 4000);
	}
	let out = "";
	try {
		out = JSON.stringify(v, null, 2);
	} catch (e) {
		out = String(s);
	}
	return out.length > 4000 ? out.slice(0, 4000) + "\n… (truncated)" : out;
}
// True only until the initial conversation load finishes — keeps the welcome
// screen from flashing on refresh before the open chat appears.
const booting = ref(true);
const showWelcome = computed(
	() => !booting.value && (!currentId.value || visibleMessages.value.length === 0)
);

// settings/overview derived metrics (all from data we already hold)
const convCount = computed(() => store.conversations.length);
const msgCount = computed(() => visibleMessages.value.length);
const toolCount = computed(() => jarvisTools.value.length);
const sessionToolCalls = computed(() =>
	Object.values(runMeta.value).reduce((s, r) => s + (r.tools || 0), 0)
);
const userMsgCount = computed(() => visibleMessages.value.filter((m) => m.role === "user").length);
const assistantMsgCount = computed(
	() => visibleMessages.value.filter((m) => m.role === "assistant").length
);
const avgTokensPerMsg = computed(() => {
	const n = msgCount.value;
	if (!usage.value || !n) return "—";
	return fmtTokens(Math.round((usage.value.chat_tokens || 0) / n));
});
const starredCount = computed(() => store.conversations.filter((c) => c.starred).length);
// Recent tool runs in this chat (most recent first), from the per-message run
// metrics we already stamp on run:end.
const recentActivity = computed(() => {
	const out = [];
	for (const m of visibleMessages.value) {
		const meta = runMeta.value[m.name];
		if (m.role === "assistant" && meta && meta.tools) {
			out.push({ tools: meta.tools, ms: meta.ms || 0, names: meta.names || [] });
		}
	}
	return out.reverse();
});
const headerSub = computed(() => {
	const n = visibleMessages.value.length;
	return n ? `${Math.ceil(n / 2)} message${n > 2 ? "s" : ""}` : "ERPNext Assistant";
});
const canSend = computed(
	() =>
		(input.value.trim().length > 0 || pendingFiles.value.length > 0) &&
		!sending.value &&
		// Suspended: the server rejects every send, so keep the button dead.
		!suspendedNotice.value &&
		// No model configured: nothing on the other end can answer, so prevent the
		// send rather than reporting the failure after the fact.
		!noAiConnected.value &&
		// A dictation that hasn't landed yet blocks the send inside send() anyway (its words
		// would be dropped). Disable the button too, with the reason on its tooltip: leaving it
		// lit and swallowing the click behind a fading toast is the button lying about what it
		// does — and record→stop→read→record-more is the flow dictation actively encourages.
		// (Enter still routes through send(), which keeps the toast for that path.)
		!voiceSendBlockReason.value
);
const busy = computed(() => sending.value || waiting.value);
// A parked write's Confirm dispatches a follow-up agent turn (continuation).
// The action:pending card is published MID-TURN (the gate parks inside the
// model's tool callback while the parent reply is still streaming), so a click
// then would run a second turn concurrent with the parent on the same
// conversation. Gate Confirm on the streaming conversation, which stays set for
// the whole run (unlike `waiting`, which clears at the first streamed token);
// the button re-enables the instant the parent turn ends. #223 review.
const convStreaming = computed(() => store.streamingConvId === currentId.value);

const suggestions = [
	{
		title: "Analyse data",
		prompt: "Which sales orders are overdue this month?",
		bg: "var(--cta-bg)",
		fg: "var(--cta)",
		icon: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 3v18h18"/><path d="M18 9l-5 5-3-3-4 4"/></svg>',
	},
	{
		title: "Take an action",
		prompt: "Draft a document for me to review",
		bg: "var(--green-bg)",
		fg: "var(--green)",
		icon: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14M5 12h14"/></svg>',
	},
	{
		title: "Search records",
		prompt: "Search for a customer or contact",
		bg: "var(--amber-bg)",
		fg: "var(--amber)",
		icon: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="7"/><path d="m21 21-4.3-4.3"/></svg>',
	},
	{
		title: "Draft content",
		prompt: "Write a follow-up email to a lead",
		bg: "rgba(139,92,246,.12)",
		fg: "#8b5cf6",
		icon: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
	},
];

// Inline action blocks the agent emits: a rich ```jarvis-action JSON card (a doc
// create/update confirm, or an email draft), or a simple ```confirm label as a
// fallback. The chat renders them as cards with buttons and strips the raw block
// from the visible prose.
const _ACTION_RE = /```jarvis-action[ \t]*\n([\s\S]*?)```/;
const _CONFIRM_RE = /```confirm[ \t]*\n([\s\S]*?)```/;
// Interactive clarifying questions: the agent emits a ```jarvis-ask JSON block
// (a list of questions, each single/multi/yesno with up to a few options); the
// chat renders it as option cards with one Submit (<AskCard>), and strips the
// raw block. Parsing lives in @/lib/chatAsk so every surface agrees.
// A ```jarvis-cards block: a list of record cards the chat renders as a
// horizontally-scrollable card strip instead of a wide Markdown table.
const _CARDS_RE = /```jarvis-cards[ \t]*\n([\s\S]*?)```/;
// The agent declares which skill(s) it used to shape a reply in a ```jarvis-skill
// block; the chat shows a small chip and strips the raw block.
const _SKILL_RE = /```jarvis-skill[ \t]*\n([\s\S]*?)```/;
// A ```jarvis-macro block: the agent proposes a reusable macro (name +
// description + ordered steps); the chat renders a "Save as macro" card that
// opens the macro editor pre-filled, and strips the raw JSON from the prose.
const _MACRO_RE = /```jarvis-macro[ \t]*\n([\s\S]*?)```/;
// A ```jarvis-chart block: a high-level chart spec the chat renders inline with
// ECharts (themed by chartTheme; the agent never sends raw ECharts options).
const _CHART_RE = /```jarvis-chart[ \t]*\n([\s\S]*?)```/g;
const _CHART_TYPES = new Set([
	"bar",
	"line",
	"area",
	"pie",
	"donut",
	"scatter",
	"bubble",
	"heatmap",
	"boxplot",
	"radar",
	"funnel",
	"gauge",
]);
// The agent keeps emitting ```mermaid xychart-beta for DATA charts instead of
// jarvis-chart. Mermaid renders xychart unstyled and crams the axis labels, so
// we intercept those blocks, parse the fixed xychart-beta grammar into a
// jarvis-chart spec, and render them through the themed ECharts path instead.
const _XYCHART_RE = /```mermaid[ \t]*\n([\s\S]*?)```/g;
function _xySplit(s) {
	const out = [];
	const re = /"([^"]*)"|'([^']*)'|([^,]+)/g;
	let m;
	while ((m = re.exec(s))) {
		const v = (m[1] ?? m[2] ?? m[3] ?? "").trim().replace(/^["']|["']$/g, "");
		if (v) out.push(v);
	}
	return out;
}
function parseXychart(body) {
	const text = String(body || "");
	if (!/^\s*xychart-beta\b/.test(text)) return null;
	const horizontal = /xychart-beta[ \t]+horizontal\b/.test(text);
	const tM = text.match(/title[ \t]+"([^"]*)"/);
	const yM = text.match(/y-axis[ \t]+"([^"]*)"/);
	const yLabel = yM ? yM[1].trim() : undefined;
	let x = [];
	const xM = text.match(/x-axis[ \t]+\[([^\]]*)\]/);
	if (xM) x = _xySplit(xM[1]);
	const series = [];
	let anyBar = false;
	const re = /\b(bar|line)[ \t]+(?:"([^"]*)"[ \t]+)?\[([^\]]*)\]/g;
	let m;
	while ((m = re.exec(text))) {
		const data = _xySplit(m[3])
			.map(Number)
			.filter((n) => !Number.isNaN(n));
		if (!data.length) continue;
		if (m[1] === "bar") anyBar = true;
		series.push({ name: m[2] || yLabel || "Value", data });
	}
	if (!series.length) return null;
	const spec = { type: anyBar ? "bar" : "line", x, series };
	if (tM) spec.title = tM[1].trim();
	if (horizontal) spec.options = { horizontal: true };
	return spec;
}
function stripBlocks(text) {
	return (text || "")
		.replace(/```jarvis-action[ \t]*\n[\s\S]*?```/g, "")
		.replace(/```confirm[ \t]*\n[\s\S]*?```/g, "")
		.replace(/```jarvis-ask[ \t]*\n[\s\S]*?```/g, "")
		.replace(/```jarvis-cards[ \t]*\n[\s\S]*?```/g, "")
		.replace(/```jarvis-skill[ \t]*\n[\s\S]*?```/g, "")
		.replace(/```jarvis-macro[ \t]*\n[\s\S]*?```/g, "")
		.replace(/```jarvis-chart[ \t]*\n[\s\S]*?```/g, "")
		.replace(/```mermaid[ \t]*\n[ \t]*xychart-beta[\s\S]*?```/g, "")
		.replace(/\n{3,}/g, "\n\n")
		.trim();
}
const _skillUsedCache = new Map();
function skillsUsedOf(m) {
	const content = (m && m.content) || "";
	if (!content.includes("jarvis-skill")) return [];
	if (_skillUsedCache.has(content)) return _skillUsedCache.get(content);
	let names = [];
	const mt = content.match(_SKILL_RE);
	if (mt) {
		names = mt[1]
			.split(/[\n,]+/)
			.map((s) => s.trim().replace(/^[-*]\s*/, ""))
			.filter(Boolean)
			.map((s) => s.replace(/^custom-/, "")) // hide the internal prefix
			.slice(0, 6);
		names = [...new Set(names)];
	}
	_skillUsedCache.set(content, names);
	return names;
}
const _cardsCache = new Map();
function cardsOf(m) {
	const content = (m && m.content) || "";
	if (!content.includes("jarvis-cards")) return null;
	if (_cardsCache.has(content)) return _cardsCache.get(content);
	let res = null;
	const mt = content.match(_CARDS_RE);
	if (mt) {
		try {
			const a = JSON.parse(mt[1].trim());
			const list = Array.isArray(a) ? a : a && a.cards;
			if (Array.isArray(list)) {
				const cards = list
					.slice(0, 60)
					.map((c) => ({
						title: String(c.title || c.name || "").trim(),
						subtitle: String(c.subtitle || "").trim(),
						doctype: String(c.doctype || "").trim(),
						name: String(c.name || "").trim(),
						fields: Array.isArray(c.fields)
							? c.fields.slice(0, 12).map((f) => ({
									label: String(f.label || ""),
									value: String(f.value != null ? f.value : ""),
							  }))
							: [],
					}))
					.filter((c) => c.title || c.fields.length);
				if (cards.length) res = { title: String((a && a.title) || ""), cards };
			}
		} catch (e) {
			res = null;
		}
	}
	_cardsCache.set(content, res);
	return res;
}
// Card-strip pagination: past a page of cards the horizontal scroll loses your
// place, so long lists page instead (‹ 1–6 of 50 ›). Page index per message.
const CARD_PAGE_SIZE = 6;
const cardPage = ref({}); // message name -> 0-based page
function cardPageOf(m) {
	return cardPage.value[m.name] || 0;
}
function pagedCards(m) {
	const cs = cardsOf(m);
	if (!cs) return [];
	const p = cardPageOf(m);
	return cs.cards.slice(p * CARD_PAGE_SIZE, (p + 1) * CARD_PAGE_SIZE);
}
function stepCardPage(m, dir) {
	const cs = cardsOf(m);
	if (!cs) return;
	const last = Math.max(0, Math.ceil(cs.cards.length / CARD_PAGE_SIZE) - 1);
	const next = Math.min(last, Math.max(0, cardPageOf(m) + dir));
	cardPage.value = { ...cardPage.value, [m.name]: next };
}
const _macroCardCache = new Map();
// "Save as macro" — the macro editor now lives on /macros, so these stash a
// draft (via macroPrefill) and navigate there; MacrosView opens it pre-filled.
const canSaveAsMacro = computed(
	() =>
		!!currentId.value &&
		messages.value.some(
			(m) => m.role === "user" && m.content && !m.content.startsWith("▶ Running macro")
		)
);
function saveConversationAsMacro() {
	const steps = messages.value
		.filter((m) => m.role === "user" && m.content && !m.content.startsWith("▶ Running macro"))
		.map((m) => ({ label: "", prompt: m.content }));
	if (!steps.length) return;
	setMacroPrefill({
		macro_name:
			currentTitle.value && currentTitle.value !== "New chat" ? currentTitle.value : "",
		steps,
	});
	router.push("/macros/new");
}
// An agent-proposed ```jarvis-macro card's "Save as macro" button.
function openMacroFromCard(card) {
	setMacroPrefill({
		macro_name: card.name || "",
		description: card.description || "",
		steps: (card.steps || []).map((s) => ({ label: s.label || "", prompt: s.prompt || "" })),
	});
	router.push("/macros/new");
}

function macroCardOf(m) {
	const content = (m && m.content) || "";
	if (!content.includes("jarvis-macro")) return null;
	if (_macroCardCache.has(content)) return _macroCardCache.get(content);
	let res = null;
	const mt = content.match(_MACRO_RE);
	if (mt) {
		try {
			const a = JSON.parse(mt[1].trim());
			const rawSteps = Array.isArray(a) ? a : a && a.steps;
			if (Array.isArray(rawSteps)) {
				const steps = rawSteps
					.slice(0, 40)
					.map((s) => ({
						label: String((s && s.label) || "").trim(),
						prompt: String((s && (s.prompt != null ? s.prompt : s)) || "").trim(),
					}))
					.filter((s) => s.prompt);
				if (steps.length) {
					res = {
						name: String((a && (a.name || a.macro_name)) || "").trim(),
						description: String((a && a.description) || "").trim(),
						steps,
					};
				}
			}
		} catch (e) {
			res = null;
		}
	}
	_macroCardCache.set(content, res);
	return res;
}
const _chartsCache = new Map();
function chartsOf(m) {
	const content = (m && m.content) || "";
	if (!content.includes("jarvis-chart") && !content.includes("xychart-beta")) return [];
	if (_chartsCache.has(content)) return _chartsCache.get(content);
	const specs = [];
	for (const mt of content.matchAll(_CHART_RE)) {
		try {
			const s = JSON.parse(mt[1].trim());
			if (
				s &&
				typeof s === "object" &&
				_CHART_TYPES.has(s.type) &&
				Array.isArray(s.series)
			) {
				specs.push(s);
			}
		} catch (e) {
			/* incomplete mid-stream JSON: skip until the closing fence arrives */
		}
	}
	for (const mt of content.matchAll(_XYCHART_RE)) {
		const s = parseXychart(mt[1]);
		if (s) specs.push(s);
	}
	_chartsCache.set(content, specs);
	return specs;
}

function askOf(m) {
	return parseAsk((m && m.content) || "");
}
function actionOf(m) {
	const mt = ((m && m.content) || "").match(_ACTION_RE);
	if (!mt) return null;
	try {
		const a = JSON.parse(mt[1].trim());
		return a && typeof a === "object" ? a : null;
	} catch (e) {
		return null;
	}
}
function confirmLabel(m) {
	const mt = ((m && m.content) || "").match(_CONFIRM_RE);
	return mt ? mt[1].trim() : "";
}
// render() is memoized so an unrelated re-render of this monolithic component
// (e.g. flipping the model menu) does NOT re-parse markdown for every visible
// message. Correctness: (a) render still reads docNameRegex.value, so Vue's
// dependency tracking is unchanged — when a tool call adds a doc ref, docNameRegex
// recomputes to a NEW RegExp instance, the identity check swaps in a fresh cache,
// and stale linkification is impossible; (b) a streaming message's content changes
// every tick, so it cache-misses exactly as today — no regression, while stable
// messages become O(1) map hits on menu toggles; (c) the 800-entry clear caps
// memory across long conversations. Keyed on the raw text string deliberately:
// identical content in two messages renders identically.
let _renderCacheRegex = undefined;
let _renderCache = new Map();
function render(text) {
	const re = docNameRegex.value;
	if (re !== _renderCacheRegex) {
		_renderCacheRegex = re;
		_renderCache = new Map();
	}
	const hit = _renderCache.get(text);
	if (hit !== undefined) return hit;
	const out = linkifyDocs(renderMarkdown(stripBlocks(text)));
	if (_renderCache.size >= 800) _renderCache.clear();
	_renderCache.set(text, out);
	return out;
}
// {document name → DocType} harvested from THIS conversation's tool calls
// (get_doc / create_doc / get_list / update_doc / …). We only ever linkify IDs
// that actually came back from a tool, so we always know the DocType for the
// Desk URL and never false-positive on arbitrary prose.
const docRefs = computed(() => {
	const map = {};
	const add = (dt, name) => {
		if (dt && typeof name === "string" && name.length >= 4) map[name] = dt;
	};
	for (const m of messages.value) {
		if (m.role !== "tool") continue;
		let args = {};
		let res = {};
		try {
			args = m.tool_args ? JSON.parse(m.tool_args) : {};
		} catch (e) {}
		try {
			res = m.tool_result ? JSON.parse(m.tool_result) : {};
		} catch (e) {}
		const dt = args.doctype;
		if (args.name) add(dt, args.name);
		const data = res && res.data;
		if (Array.isArray(data)) {
			for (const row of data) if (row && row.name) add(row.doctype || dt, row.name);
		} else if (data && typeof data === "object") {
			add(data.doctype || dt, data.name);
		}
	}
	return map;
});
const _escapeRegex = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
// Compiled once per docRefs change: matches any known doc name as a whole token
// (not a substring of a longer id/word). Capped so a huge get_list can't build a
// pathological alternation.
const docNameRegex = computed(() => {
	const names = Object.keys(docRefs.value)
		.sort((a, b) => b.length - a.length)
		.slice(0, 400);
	if (!names.length) return null;
	try {
		return new RegExp(`(?<![\\w-])(${names.map(_escapeRegex).join("|")})(?![\\w-])`, "g");
	} catch (e) {
		return null; // e.g. a browser without lookbehind — degrade to no links
	}
});
const _deskSlug = (dt) => dt.toLowerCase().replace(/ /g, "-");
// Turn known document IDs in the rendered markdown HTML into Desk links, without
// touching text already inside an <a> (so markdown links stay intact).
function linkifyDocs(html) {
	const re = docNameRegex.value;
	const refs = docRefs.value;
	if (!re || !html) return html;
	let inAnchor = 0;
	return html.replace(
		/(<a\b[^>]*>)|(<\/a>)|(<[^>]+>)|([^<]+)/gi,
		(m, aOpen, aClose, otherTag, text) => {
			if (aOpen) {
				inAnchor++;
				return aOpen;
			}
			if (aClose) {
				inAnchor = Math.max(0, inAnchor - 1);
				return aClose;
			}
			if (otherTag != null) return otherTag;
			if (inAnchor) return text;
			return text.replace(re, (name) => {
				const dt = refs[name];
				if (!dt) return name;
				const url = `/app/${_deskSlug(dt)}/${encodeURIComponent(name)}`;
				return `<a href="${url}" target="_blank" rel="noopener" class="jv-doclink" title="Open ${dt} in ERPNext">${name}</a>`;
			});
		}
	);
}
// The last assistant message (finished, turn idle) decides which card is live —
// once the user clicks, a new message lands and the card retires automatically.
const _lastAssistant = computed(() => {
	if (busy.value) return null;
	// visibleMessages now also carries receipt-chip tool rows (a confirmed /
	// discarded / failed gated write), and a discard fires no continuation, so a
	// chip can be the tail. Scan back past any trailing tool rows to the real
	// last turn message before deciding which assistant owns the live card.
	const vm = visibleMessages.value;
	let last = null;
	for (let i = vm.length - 1; i >= 0; i--) {
		if (vm[i].role === "tool") continue;
		last = vm[i];
		break;
	}
	return last && last.role === "assistant" && !last.streaming ? last : null;
});
const activeAction = computed(() =>
	_lastAssistant.value ? actionOf(_lastAssistant.value) : null
);
const actionFor = computed(() => (activeAction.value ? _lastAssistant.value.name : null));
const confirmFor = computed(() =>
	_lastAssistant.value && !activeAction.value && confirmLabel(_lastAssistant.value)
		? _lastAssistant.value.name
		: null
);
function actionSend(text) {
	send(text);
}
function answerConfirm(ok, label) {
	// Echo the card's own wording so the transcript reads like what the user
	// clicked ("Yes — Confirm and save") instead of a canned "go ahead".
	const l = (label || "").trim();
	send(ok ? (l ? `Yes — ${l}` : "Yes, go ahead.") : "No, cancel that.");
}

// --- Field-control helpers shared by the confirm card and the record draft
// panel (chip → side panel; see _formMeta / openDraftPanel below).
function _isLongVal(v) {
	const s = String(v == null ? "" : v);
	return s.length > 55 || s.includes("\n");
}
// Map a Frappe fieldtype → the edit control to render + its options payload.
function _controlFor(fieldtype, options) {
	switch (fieldtype) {
		case "Link":
			return ["link", options || ""]; // options = target doctype (searchLink)
		case "Select":
			return [
				"select",
				String(options || "")
					.split("\n")
					.map((o) => o.trim()),
			];
		case "Check":
			return ["check", ""];
		case "Date":
			return ["date", ""];
		case "Datetime":
			return ["datetime", ""];
		case "Time":
			return ["time", ""];
		case "Int":
		case "Float":
		case "Currency":
		case "Percent":
		case "Rating":
			return ["number", ""];
		case "Small Text":
		case "Text":
		case "Long Text":
		case "Code":
		case "Text Editor":
		case "HTML Editor":
		case "Markdown Editor":
		case "JSON":
			return ["text", ""];
		default:
			return ["data", ""];
	}
}
// "Item Group" / "item_group" / "itemGroup" all → "itemgroup": the agent's
// action JSON labels fields sometimes by display label, sometimes by fieldname.
function _normKey(s) {
	return String(s || "")
		.toLowerCase()
		.replace(/[^a-z0-9]/g, "");
}
// Resolve one card label to a field: exact normalized match on label/fieldname,
// else unique containment (e.g. "UOM" → stock_uom when it's the only sensible
// hit; required fields win ambiguous shorthands).
function _actField(meta, label) {
	const k = _normKey(label);
	if (!k) return null;
	if (meta.map[k]) return meta.map[k];
	const hits = meta.fields.filter((f) => f._kl.includes(k) || f._kf.includes(k));
	if (hits.length === 1) return hits[0];
	if (hits.length > 1) {
		const reqd = hits.filter((f) => f.reqd);
		if (reqd.length === 1) return reqd[0];
	}
	return null;
}
function _checkToYesNo(v) {
	const s = typeof v === "string" ? v.toLowerCase() : v;
	return ["1", 1, "yes", "true", true, "on"].includes(s) ? "Yes" : "No";
}
// --- Record draft panel: the action JSON is the draft; edits are local; apply
// posts to actions_api (no LLM round-trip). ---
const draftPanel = ref(null);

// Resizable edit panel — drag the inner (left) edge. Clamped to [min, max],
// snaps to the default within +/-10px, width persisted to localStorage. Scoped
// to the draft panel only (the artifact viewer shares .jv-artifact-panel but
// keeps its CSS width, since this width is applied inline on the draft aside).
const DRAFT_DEFAULT_WIDTH = 720;
const DRAFT_MIN_WIDTH = 440;
const DRAFT_WIDTH_KEY = "jarvis-draftpanel-width";
const draftMaxWidth = () => Math.min(1100, Math.round(window.innerWidth * 0.92));
const clampDraftWidth = (w) => Math.min(draftMaxWidth(), Math.max(DRAFT_MIN_WIDTH, w));
const _storedDraftW = Number(localStorage.getItem(DRAFT_WIDTH_KEY));
const draftPanelWidth = ref(clampDraftWidth(_storedDraftW || DRAFT_DEFAULT_WIDTH));
const draftPanelEl = ref(null);
const draftResizing = ref(false);
let _draftPanelRight = 0;

function onDraftResizeMove(e) {
	draftResizing.value = true;
	let w = _draftPanelRight - e.clientX;
	if (w > DRAFT_DEFAULT_WIDTH - 10 && w < DRAFT_DEFAULT_WIDTH + 10) w = DRAFT_DEFAULT_WIDTH;
	draftPanelWidth.value = clampDraftWidth(w);
}
function onDraftResizeUp() {
	document.body.classList.remove("jv-col-resizing");
	localStorage.setItem(DRAFT_WIDTH_KEY, String(draftPanelWidth.value));
	draftResizing.value = false;
	document.removeEventListener("mousemove", onDraftResizeMove);
	document.removeEventListener("mouseup", onDraftResizeUp);
}
function startDraftResize() {
	// capture the panel's right edge so width is correct regardless of overlay offset
	_draftPanelRight = draftPanelEl.value
		? draftPanelEl.value.getBoundingClientRect().right
		: window.innerWidth;
	document.body.classList.add("jv-col-resizing");
	document.addEventListener("mousemove", onDraftResizeMove);
	document.addEventListener("mouseup", onDraftResizeUp);
}
onBeforeUnmount(() => {
	document.removeEventListener("mousemove", onDraftResizeMove);
	document.removeEventListener("mouseup", onDraftResizeUp);
});

// Backdrop-click dismiss must ignore the tail of an in-panel drag. When a resize
// drag starts on a panel's handle (inside the panel) and the mouse is released
// over the dimmed backdrop — which is exactly what stretching the panel does —
// the browser dispatches the `click` on the common ancestor of the down/up
// targets, i.e. the overlay. A bare @click.self would then read that
// drag-release as an outside-click and close the panel. Gate the close on the
// press ALSO having started on the backdrop (recorded on the overlay's own
// mousedown; @click.self already guarantees the release resolved to it).
let _overlayPressOnBackdrop = false;
function onOverlayMouseDown(e) {
	_overlayPressOnBackdrop = e.target === e.currentTarget;
}
function onOverlayBackdropClick(close) {
	if (_overlayPressOnBackdrop) close();
}

// one shared link-search menu for panel inputs, keyed "f:<fieldname>" or "t:<ti>:<ri>:<col>"
const draftLink = ref({ key: "", items: [], open: false, up: false });
const _formMetaCache = {};

async function _formMeta(doctype) {
	if (_formMetaCache[doctype]) return _formMetaCache[doctype];
	const r = await api.getDoctypeFormMeta(doctype);
	if (!r || !r.ok) throw new Error("no form meta");
	for (const f of r.fields) {
		f._kl = _normKey(f.label);
		f._kf = _normKey(f.fieldname);
	}
	_formMetaCache[doctype] = r;
	return r;
}

// Native date/time inputs REQUIRE canonical values (yyyy-mm-dd / yyyy-mm-ddThh:mm);
// anything else — "2026-07-10 00:00:00", "10-07-2026" — renders the input EMPTY,
// which read as "the date isn't picking". Normalize whatever the agent/doc gave us.
function _normDateVal(fieldtype, v) {
	const s = String(v == null ? "" : v).trim();
	if (!s) return s;
	if (fieldtype === "Date") {
		let m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
		if (m) return `${m[1]}-${m[2]}-${m[3]}`;
		m = s.match(/^(\d{2})[-/](\d{2})[-/](\d{4})$/); // dd-mm-yyyy / dd/mm/yyyy
		if (m) return `${m[3]}-${m[2]}-${m[1]}`;
	}
	if (fieldtype === "Datetime") {
		let m = s.match(/^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})/);
		if (m) return `${m[1]}T${m[2]}`;
		m = s.match(/^(\d{4}-\d{2}-\d{2})$/);
		if (m) return `${m[1]}T00:00`;
	}
	if (fieldtype === "Time") {
		const m = s.match(/^(\d{2}:\d{2})/);
		if (m) return m[1];
	}
	return s;
}
function _panelField(metaField, value) {
	let [control, options] = _controlFor(metaField.fieldtype, metaField.options);
	let v = value == null ? "" : String(value);
	if (["date", "datetime", "time"].includes(control)) v = _normDateVal(metaField.fieldtype, v);
	let orig = v;
	if (control === "check") {
		v = _checkToYesNo(v);
		orig = v;
	}
	if (control === "select" && Array.isArray(options) && v && !options.includes(v))
		options = [v, ...options];
	return {
		fieldname: metaField.fieldname,
		label: metaField.label,
		control,
		options,
		fieldtype: metaField.fieldtype,
		reqd: metaField.reqd,
		read_only: metaField.read_only,
		value: v,
		orig,
	};
}

// Build the draft model from an action + form meta (+ live doc for updates),
// WITHOUT opening the panel - so the editable panel and later summary/preview
// surfaces can share one construction path.
async function buildDraftModel(a) {
	if (!a || a.kind !== "doc" || !a.doctype) return;
	const verb = a.verb === "update" ? "update" : "create";
	// An action block we cannot render must FAIL, not render empty - a blank card
	// reads as "Jarvis did not try" rather than "Jarvis emitted a shape I cannot
	// draw". `docs` is a create_doc batch payload, not an action key (AGENTS.md:122
	// documents fields+tables for ONE record) - throw on it for EITHER verb: a
	// {"verb":"update","docs":[...]} fusion otherwise builds an empty diff, renders
	// "No field changes." with Confirm ENABLED, and sends update_doc(dt, "", {}).
	// A hybrid carrying BOTH fields and docs throws too: rendering the fields and
	// dropping the docs is a half-card the user confirms believing it is the set.
	if (Array.isArray(a.docs)) {
		const err = new Error(
			"This draft carries a `docs` batch, which is a create_doc payload rather than a card. Ask me to apply them as a batch."
		);
		err.jvUserMessage = err.message;
		throw err;
	}
	// Create-only: a fieldless UPDATE block legitimately renders "No field changes.",
	// and a tables-only create is legal, so neither may throw.
	if (verb === "create" && !(a.fields || []).length && !(a.tables || []).length) {
		const err = new Error("This draft has no fields to show.");
		err.jvUserMessage = err.message;
		throw err;
	}
	let meta;
	try {
		meta = await _formMeta(a.doctype);
	} catch (e) {
		return;
	} // no meta → no panel (old card still shows)
	let base = { values: {}, tables: {} };
	if (verb === "update" && a.name) {
		try {
			base = await api.loadDocForEdit(a.doctype, a.name);
		} catch (e) {
			/* not editable → create-style view */
		}
	}
	// proposed main fields: agent's fields resolved by label-or-fieldname
	const metaLookup = { fields: meta.fields, map: {} };
	for (const f of meta.fields) {
		if (f._kf && !metaLookup.map[f._kf]) metaLookup.map[f._kf] = f;
		if (f._kl) metaLookup.map[f._kl] = f;
	}
	const proposed = {}; // fieldname -> value
	for (const f of a.fields || []) {
		const m = _actField(metaLookup, f.label);
		if (m && m.fieldtype !== "Table") proposed[m.fieldname] = f.value;
	}
	const fields = [];
	const seen = new Set();
	for (const f of meta.fields) {
		if (f.fieldtype === "Table") continue;
		const has = f.fieldname in proposed;
		const baseV = base.values[f.fieldname];
		// Show: agent-proposed fields + required fields + (update) filled fields the agent referenced
		if (!has && !f.reqd) continue;
		const pf = _panelField(f, has ? proposed[f.fieldname] : baseV);
		if (verb === "update")
			pf.orig =
				baseV == null ? "" : String(pf.control === "check" ? _checkToYesNo(baseV) : baseV);
		pf.changed = verb === "update" && String(pf.value) !== String(pf.orig);
		fields.push(pf);
		seen.add(f.fieldname);
	}
	// child tables: every meta table that is required, agent-proposed, or (update) non-empty
	const tables = [];
	const aTables = {};
	for (const t of a.tables || []) if (t && t.fieldname) aTables[t.fieldname] = t.rows || [];
	for (const [tf, spec] of Object.entries(meta.tables || {})) {
		const metaField = meta.fields.find((f) => f.fieldname === tf) || { reqd: 0 };
		const proposedRows = aTables[tf];
		const baseRows = (base.tables || {})[tf] || [];
		if (!proposedRows && !metaField.reqd && !baseRows.length) continue;
		// columns = child meta columns ∪ keys the agent used (unknown keys → data input)
		const columns = spec.columns.slice();
		const known = new Set(columns.map((c) => c.fieldname));
		for (const r of proposedRows || []) {
			for (const k of Object.keys(r)) {
				if (!known.has(k)) {
					known.add(k);
					columns.push({
						fieldname: k,
						label: k,
						fieldtype: "Data",
						options: "",
						reqd: 0,
						read_only: 0,
					});
				}
			}
		}
		const srcRows = proposedRows != null ? proposedRows : baseRows; // proposal REPLACES loaded rows
		const rows = srcRows.map((r) => {
			const o = {};
			for (const c of columns)
				o[c.fieldname] = _normDateVal(
					c.fieldtype,
					r[c.fieldname] == null ? "" : String(r[c.fieldname])
				);
			return o;
		});
		if (!rows.length) rows.push(_blankRow(columns));
		tables.push({
			fieldname: tf,
			label: spec.label,
			child: spec.child_doctype,
			columns,
			rows,
			origJson: JSON.stringify(verb === "update" ? baseRows : null),
		});
	}
	return {
		verb,
		doctype: a.doctype,
		docName: verb === "update" ? a.name || "" : "",
		title: a.title || "",
		titleField: meta.title_field || "",
		submittable: !!meta.is_submittable,
		// Multi-step plans: the card marks non-final steps "continue": 1; the
		// bench then dispatches a follow-up agent turn after Apply so the agent
		// stages the next step without the user typing "continue".
		cont: a.continue ? 1 : 0,
		fields,
		tables,
		tableMeta: meta.tables || {},
		applying: false,
		error: "",
		updatedToast: false,
	};
}

// Open the editable panel for an action, via the shared buildDraftModel.
async function openDraftPanel(a) {
	let model;
	// Deliberate: swallow to the SAME dead-end the old `if (!model) return` gave, so
	// a guard throw cannot escape a @click handler. The summary card already shows
	// the error. Do not "fix" this into a rethrow.
	try {
		model = await buildDraftModel(a);
	} catch (e) {
		return;
	}
	if (!model) return;
	draftPanel.value = model;
}

function _blankRow(columns) {
	const o = {};
	for (const c of columns) o[c.fieldname] = "";
	return o;
}
function addDraftRow(ti) {
	const t = draftPanel.value.tables[ti];
	t.rows.push(_blankRow(t.columns));
}
function removeDraftRow(ti, ri) {
	draftPanel.value.tables[ti].rows.splice(ri, 1);
}
function closeDraftPanel() {
	draftPanel.value = null;
	draftLink.value = { key: "", items: [], open: false, up: false };
}

// Link search shared by panel fields + grid cells.
async function onDraftLink(key, target, doctype, ev) {
	let up = false;
	const el = ev && ev.target;
	if (el && el.getBoundingClientRect)
		up = el.getBoundingClientRect().bottom > window.innerHeight - 260;
	draftLink.value = { key, items: [], open: true, up };
	if (!doctype) return;
	try {
		const r = await api.searchLink(doctype, target());
		if (draftLink.value.key !== key) return; // user moved on
		draftLink.value = {
			key,
			items: (r || [])
				.map((x) => ({ value: x.value, label: x.description || "" }))
				.slice(0, 8),
			open: true,
			up,
		};
	} catch (e) {
		/* menu stays empty */
	}
}
function pickDraftLink(setter, item) {
	setter(item.value);
	draftLink.value = { key: "", items: [], open: false, up: false };
}
function closeDraftLink() {
	setTimeout(() => {
		draftLink.value = { ...draftLink.value, open: false };
	}, 160);
}

// est. totals: any grid with qty (+rate) columns
// Auto-derived, read-only totals for the edit panel: sum the line-item qty and
// qty×rate across every child table that has a `qty` column. Returns a
// structured object (not a string) so the UI can render each figure as its own
// read-only field; null when there's nothing to total.
const draftTotals = computed(() => {
	const p = draftPanel.value;
	if (!p) return null;
	let qty = 0,
		amt = 0,
		hasQty = false,
		hasAmt = false;
	for (const t of p.tables) {
		const q = t.columns.find((c) => c.fieldname === "qty");
		const r = t.columns.find((c) => c.fieldname === "rate");
		if (!q) continue;
		hasQty = true;
		for (const row of t.rows) {
			const n = parseFloat(row.qty) || 0;
			qty += n;
			if (r) {
				hasAmt = true;
				amt += n * (parseFloat(row.rate) || 0);
			}
		}
	}
	if (!hasQty) return null;
	return {
		qty: qty.toLocaleString("en-IN"),
		amount: amt.toLocaleString("en-IN", {
			minimumFractionDigits: 2,
			maximumFractionDigits: 2,
		}),
		hasAmt,
	};
});
const draftCta = computed(() => {
	const p = draftPanel.value;
	if (!p) return "";
	return p.verb === "update" ? `Update ${p.docName || p.doctype}` : `Create ${p.doctype}`;
});
// Summary-first: the read-only model + view for the current create/update action.
const summaryState = ref({ key: "", model: null, view: null, error: "" });
async function ensureActionSummary(a) {
	const key = JSON.stringify([
		a.verb || "create",
		a.doctype,
		a.name || "",
		a.fields || [],
		a.tables || [],
	]);
	if (summaryState.value.key === key) return;
	summaryState.value = { key, model: null, view: null, error: "" };
	let model;
	try {
		model = await buildDraftModel({ verb: a.verb || "create", ...a });
	} catch (e) {
		const msg = (e && e.jvUserMessage) || "Could not load this draft. Tell me to try again.";
		if (summaryState.value.key === key)
			summaryState.value = { key, model: null, view: null, error: msg };
		return;
	}
	if (!model) {
		if (summaryState.value.key === key)
			summaryState.value = {
				key,
				model: null,
				view: null,
				error: "Could not load this draft. Tell me to try again.",
			};
		return;
	}
	if (summaryState.value.key !== key) return; // a newer action superseded this build
	summaryState.value = { key, model, view: summarize(model, a), error: "" };
}
function isEditVerb(a) {
	return !!a && (!a.verb || a.verb === "create" || a.verb === "update");
}
async function confirmSummary() {
	const model = summaryState.value.model;
	if (!model || model.applying || convStreaming.value) return;
	await applyDraft(0, model);
}

// Read-only preview: opens DraftPreview over the current summary's model.
const previewOpen = ref(false);
function previewSummary() {
	if (!summaryState.value.model) return;
	previewOpen.value = true;
}
async function onPreviewConfirm() {
	previewOpen.value = false;
	await confirmSummary();
}
function onPreviewEdit() {
	previewOpen.value = false;
	const a = activeAction.value;
	if (a) openDraftPanel({ verb: a.verb || "create", ...a });
}

// Summary-first default (Task 1.3): a fresh create/update action builds the
// read-only summary card, NOT the editable panel. If the panel is already
// open (the user clicked Edit) and the action re-emits, refresh it in place
// - this also covers loading an old conversation that ends on a pending
// draft while mid-edit.
watch(actionFor, () => {
	const a = activeAction.value;
	if (!(a && a.kind === "doc" && (a.verb === "create" || a.verb === "update" || !a.verb)))
		return;
	ensureActionSummary(a); // keep the summary card in sync with the latest action
	if (draftPanel.value) {
		// panel is open (user is editing) and the action re-emitted -> refresh it too
		openDraftPanel({ verb: a.verb || "create", ...a }).then(() => {
			if (draftPanel.value) draftPanel.value.updatedToast = true;
		});
	}
});

// --- apply wiring: draft panel create/update round-trip via apply_action ---

function _coerceOut(f) {
	if (f.control === "check") return f.value === "Yes" ? 1 : 0;
	if (f.control === "number") return f.value === "" ? "" : Number(f.value);
	return f.value;
}
function _coerceRow(t, r) {
	const out = {};
	for (const c of t.columns) {
		if (c.read_only) continue;
		let v = r[c.fieldname];
		if (v === "" || v == null) continue;
		if (["Int", "Float", "Currency", "Percent"].includes(c.fieldtype)) v = Number(v);
		if (c.fieldtype === "Check") v = Number(v) ? 1 : 0;
		out[c.fieldname] = v;
	}
	return out;
}

async function applyDraft(submitFlag, model = draftPanel.value) {
	const p = model;
	if (!p || p.applying) return;
	const values = {};
	for (const f of p.fields) {
		if (f.read_only) continue;
		const changed = String(f.value) !== String(f.orig);
		if (p.verb === "create" ? String(f.value).trim() !== "" : changed)
			values[f.fieldname] = _coerceOut(f);
	}
	for (const t of p.tables) {
		const rows = t.rows.map((r) => _coerceRow(t, r)).filter((r) => Object.keys(r).length);
		if (p.verb === "create") {
			if (rows.length) values[t.fieldname] = rows;
		} else if (
			JSON.stringify(rows) !==
			JSON.stringify(
				(JSON.parse(t.origJson) || []).map((r) =>
					_coerceRow(
						t,
						Object.fromEntries(
							Object.entries(r).map(([k, v]) => [k, v == null ? "" : String(v)])
						)
					)
				)
			)
		) {
			values[t.fieldname] = rows;
		}
	}
	p.applying = true;
	p.error = null;
	try {
		const r = await api.applyAction({
			verb: p.verb,
			doctype: p.doctype,
			name: p.docName || "",
			values,
			submit: submitFlag ? 1 : 0,
			conversation: currentId.value || "",
			continue: p.cont ? 1 : 0,
		});
		if (r && r.ok === false) {
			// apply_action now returns the enriched {ok:false, error} envelope
			// (rich detail + hint, and nothing was saved) instead of throwing a
			// raw Frappe 403/417. Keep the panel open so the values are editable.
			p.applying = false;
			p.error = r.error || { message: "Could not save — check the values." };
			return;
		}
		closeDraftPanel();
		await loadConversation(currentId.value);
		store.loadConversations();
		// SUX-3/SUXI-2: the continuation turn queued — show the chip + lock the
		// composer instead of the card vanishing into silence.
		if (r && r.queued) {
			queuedTurn.value = {
				run_id: r.run_id,
				message_id: r.message_id,
				position: r.queued_position || null,
			};
			sending.value = true;
		}
	} catch (e) {
		p.applying = false;
		p.error = {
			message:
				(e && e.messages && e.messages[0]) ||
				(e && e.message) ||
				"Could not save — check the values.",
		};
	}
}

function discardDraft() {
	closeDraftPanel();
	send("No, cancel that.");
}

// --- action:pending confirm card (write-safety gate, issue #186) ---
// A gated ERP write (create/update/submit/cancel/delete/amend/run_method/
// send_email) is parked server-side; the owner gets a realtime action:pending
// event carrying a one-time token. confirm_tool(token) is the ONLY path that
// runs the parked call. Discard just drops the card - the token expires server
// side, no backend call needed.
// A QUEUE of parked confirmations, keyed by token (issue #186, R3 fix for #4):
// a single turn can park more than one gated write, and each keeps its OWN
// confirmable card + busy/error state. Each item:
// { conversation, token, tool, summary, preview, run_id, busy, error }.
const pendingActions = ref([]);

// Only the cards belonging to the conversation on screen render (a parked write
// from another chat must not show here). The queue is already pruned to the
// current conversation on load, but filter defensively for the template v-for.
const visiblePendingActions = computed(() =>
	pendingActions.value.filter((pa) => pa.conversation === currentId.value)
);

// A legacy container (persona v0.39, pre write-safety) may still emit a
// jarvis-action card for a gated write verb / an email whose own action button
// was removed. During the rollout window we render this note in place of the
// dead button instead of a card that can never act (R3 fix for #12/#13).
const LEGACY_GATE_NOTE =
	"Waiting for the confirmation card. If it does not appear shortly, ask me to try again.";

// The card's headline: the event's own summary, or the described-intent's.
function pendingSummaryOf(pa) {
	if (!pa) return "";
	return pa.summary || (pa.preview && pa.preview.summary) || pa.tool || "";
}
// The "will send/execute on confirm" caption carried on either preview shape.
function pendingNoteOf(pa) {
	const pv = pa && pa.preview;
	return (pv && pv.note) || "";
}
// For a previewable dry-run, show the would-be result; described-intent
// (send_email) has no dry-run doc, so nothing extra to dump.
function pendingPreviewOf(pa) {
	const pv = pa && pa.preview;
	if (!pv || pv.described) return "";
	const w = pv.would;
	if (w == null) return "";
	return typeof w === "string" ? w : prettyJson(w);
}
// A create_docs card renders as bullet lines (one per created record) rather than
// a raw JSON dump. Returns null for every other tool, so the <pre> fallback runs.
// The model-authored notes are NOT rendered here - see batchFromPreview.
function pendingBatchOf(pa) {
	if (!pa || pa.tool !== "create_docs") return null;
	return batchFromPreview(pa.preview);
}
// The raw dry-run preview JSON, shown behind the card's Details expander (the
// same string the pre-F9 card dumped directly).
function pendingDetailsOf(pa) {
	return pendingPreviewOf(pa);
}
// Wall-clock expiry (F15): a coarse tick flips a card to its "expired" state once
// the 15-min token TTL lapses, so a stale card stops looking actionable. Real
// enforcement stays server-side (confirming an expired token fails).
const pendingNowMs = ref(Date.now());
let _expiryTick = null;
onMounted(() => {
	_expiryTick = setInterval(() => {
		pendingNowMs.value = Date.now();
	}, 15000);
	// Phase-0 admission: refresh the queued chip's position when the tab
	// regains focus (poll-on-focus fallback for a missed queue:position push).
	window.addEventListener("focus", refreshQueuePositionOnFocus);
});
onUnmounted(() => {
	if (_expiryTick) clearInterval(_expiryTick);
	window.removeEventListener("focus", refreshQueuePositionOnFocus);
});
function pendingExpiredOf(pa) {
	return pendingExpiry(pa && pa.expires_at, pendingNowMs.value).expired;
}
// Drop one card from the queue by its token (confirm-success / discard / expiry).
function removePending(token) {
	if (!token) return;
	pendingActions.value = pendingActions.value.filter((x) => x.token !== token);
}
// Enqueue a parked confirmation, deduped by token (a resync + a live event can
// both carry the same card).
function enqueuePending(card) {
	if (!card || !card.token) return;
	if (pendingActions.value.some((x) => x.token === card.token)) return;
	pendingActions.value.push({
		conversation: card.conversation,
		token: card.token,
		tool: card.tool,
		summary: card.summary || "",
		preview: card.preview || null,
		run_id: card.run_id || null,
		expires_at: card.expires_at || null,
		busy: false,
		error: null,
	});
}

async function confirmPending(pa) {
	if (!pa || pa.busy) return;
	// Defense in depth behind the disabled button: never dispatch a continuation
	// turn while the parent turn is still streaming this conversation (#223).
	if (convStreaming.value) return;
	// This confirm acts on THIS card's specific token. A realtime action:pending
	// event or a resync can add/remove other cards while the request is in flight
	// - only the same-token card's state is touched on resolve, so a slow older
	// call can never clear/error a different unconfirmed card (in-flight-race
	// guard, R1/Task7).
	const token = pa.token;
	const cardById = () => pendingActions.value.find((x) => x.token === token);
	pa.busy = true;
	pa.error = null;
	try {
		const r = await api.confirmTool(token, pa.conversation || currentId.value || "");
		if (r && r.ok === false) {
			// Token gone/expired/used, or the executed tool reported failure. Either
			// way the card is spent - surface a brief note and dismiss.
			if (r.error && r.error.type === "InvalidConfirmation") {
				removePending(token);
				// InvalidConfirmation is deliberately opaque (expired / used in another
				// tab / a redis blip). Use the card's own wall-clock expiry for the right
				// message instead of always blaming expiry (F15).
				const expired = pendingExpiry(pa.expires_at, Date.now()).expired;
				notify(
					expired
						? "This confirmation expired — tell me the action again to retry it."
						: "Couldn't confirm — it may have been handled in another tab. Refresh, or ask me to try again.",
					{ type: "error" }
				);
				return;
			}
			// The token was consumed and the tool failed; confirm_tool already
			// persisted a durable "failed" receipt chip. Drop the card and reload
			// so the chip shows in the thread instead of a lingering spent card.
			removePending(token);
			await loadConversation(currentId.value);
			store.loadConversations();
			return;
		}
		// Success - the executed result surfaces via the turn's normal tool/stream
		// events; reload to be sure the transcript reflects it (same as applyDraft).
		removePending(token);
		await loadConversation(currentId.value);
		store.loadConversations();
		// SUX-3/SUXI-2: the continuation turn queued (all slots taken). Show the
		// standard queued chip + lock the composer so the card doesn't vanish into
		// silence after the "Change saved" ack clears (mirrors send()'s r.queued).
		if (r && r.queued) {
			queuedTurn.value = {
				run_id: r.run_id,
				message_id: r.message_id,
				position: r.queued_position || null,
			};
			sending.value = true;
		}
	} catch (e) {
		const card = cardById();
		if (card)
			card.error = {
				message:
					(e && e.messages && e.messages[0]) || (e && e.message) || "Could not confirm.",
			};
	} finally {
		const card = cardById();
		if (card) card.busy = false;
	}
}
// Dismiss: consume the token server-side (closes the 15-min replay window and
// stops the card re-surfacing on reload), leave a durable "discarded" receipt
// chip, and queue a note so the agent's next turn learns it was vetoed. Fires NO
// agent turn. Best-effort: even if the call fails, the card drops locally (the
// token TTL-expires) - only the chip would be missing.
async function discardPending(pa) {
	if (!pa || pa.busy) return;
	const token = pa.token;
	const conv = pa.conversation || currentId.value || "";
	pa.busy = true;
	try {
		await api.dismissTool(token, conv);
	} catch (e) {
		// Swallow - drop the card regardless (the token self-expires server-side).
	}
	removePending(token);
	// Re-fetch so the durable "discarded" chip shows in the thread.
	await loadConversation(currentId.value);
	store.loadConversations();
}

// Resync (R3 fix for #3): re-fetch the current conversation's live parked
// confirmations so a reload / reconnect re-surfaces the cards that a realtime
// action:pending event delivered before the page was open. Deduped by token
// against whatever is already queued; freshness-guarded against a mid-flight
// conversation switch.
async function resyncPendingConfirmations(id) {
	if (!id) return;
	let items = [];
	try {
		const r = await api.listPendingConfirmations(id);
		items = (r && r.data && r.data.pending) || [];
	} catch (e) {
		return;
	}
	if (currentId.value !== id) return; // navigated away while the request was in flight
	if (!Array.isArray(items)) return;
	for (const it of items) {
		enqueuePending({
			conversation: it.conversation || id,
			token: it.token,
			tool: it.tool,
			summary: it.summary || "",
			preview: it.preview || null,
			run_id: it.run_id || null,
			expires_at: it.expires_at || null,
		});
	}
}

// Resync the queued chip from server truth (SUXI-1). The chip + its Cancel
// affordance are otherwise client-only state, lost on reload / conversation
// switch / a second tab / a WS reconnect — leaving a still-queued turn with no
// visible position and no way to cancel, and a re-enabled empty composer that
// invites a duplicate send. Mirrors resyncPendingConfirmations: called from
// loadConversation. Repopulates queuedTurn (and keeps the composer locked) when
// the conversation's own turn is queued; a dispatching turn is covered by
// loadConversation's existing streaming resume.
async function resyncQueuedTurn(id) {
	if (!id) return;
	let active = null;
	try {
		const r = await api.getActiveTurn(id);
		active = (r && r.ok && r.active) || null;
	} catch (e) {
		return; // best-effort — never block the load
	}
	if (currentId.value !== id) return; // navigated away while the request was in flight
	// SUXF-3: keep the chip (and the composer lock) through the whole pre-stream
	// window — queued AND the new preparing/ready stages — so a reload during
	// prompt-assembly/session-bootstrap shows "Starting…" rather than going silent
	// and re-enabling an empty composer that could invite a duplicate send. Once the
	// turn is dispatching/streaming, loadConversation's streaming resume owns it.
	if (active && ["queued", "preparing", "ready"].includes(active.state)) {
		queuedTurn.value = {
			run_id: active.run_id,
			message_id: active.message_id,
			position: active.position || null,
			state: active.state,
		};
		// Keep the composer locked while a turn is pre-stream so the re-enabled empty
		// composer can't invite a duplicate send.
		sending.value = true;
		waiting.value = false;
	} else if (queuedTurn.value) {
		// The turn we were showing has since dispatched/settled — drop the chip;
		// the streaming/run:end events reconcile the rest.
		queuedTurn.value = null;
	}
}

// --- interactive clarifying questions (card on the last assistant message) ---
const activeAsk = computed(() =>
	_lastAssistant.value && !activeAction.value ? askOf(_lastAssistant.value) : null
);
const askFor = computed(() => (activeAsk.value ? _lastAssistant.value.name : null));
// The draft state, readiness and answer formatting live in <AskCard> +
// @/lib/chatAsk, shared with the Dashboards builder pane.
function copyText(t) {
	const s = t || "";
	// navigator.clipboard only exists in a secure context (https / localhost).
	// Over plain http (e.g. jarvis-test.localhost) it's undefined, so the old
	// `navigator.clipboard?.writeText` silently did nothing — that's why Copy
	// "didn't work". Fall back to the legacy execCommand path in that case.
	try {
		if (navigator.clipboard && window.isSecureContext) {
			navigator.clipboard.writeText(s).catch(() => fallbackCopy(s));
			return;
		}
	} catch (e) {
		/* fall through to legacy copy */
	}
	fallbackCopy(s);
}
function fallbackCopy(s) {
	try {
		const ta = document.createElement("textarea");
		ta.value = s;
		ta.setAttribute("readonly", "");
		ta.style.position = "fixed";
		ta.style.top = "-9999px";
		document.body.appendChild(ta);
		ta.select();
		ta.setSelectionRange(0, s.length);
		document.execCommand("copy");
		document.body.removeChild(ta);
	} catch (e) {
		/* clipboard truly unavailable */
	}
}
// Per-message timestamp (hover-revealed with the msgbar; full date on its own
// hover). Server rows carry a site-tz `creation` (rendered via utils/datetime's
// dayjsLocal path); optimistic tmp rows carry `creation_browser` (a local
// epoch) until the server copy reconciles them.
//
// An assistant row's `creation` is stamped at run start (≈ the user's send
// time), so showing it makes the reply look like it landed the instant the
// question was sent. The reply's real "appeared on screen" moment is its
// `modified` — the timestamp of the final streamed-content write (the same
// span elapsedOf() treats as the generation duration). So replies show
// `modified`; user rows keep `creation` (their send time).
function msgStamp(m) {
	if (m.role === "assistant" && m.modified) return m.modified;
	return m.creation;
}
function msgTime(m) {
	const ts = msgStamp(m);
	if (ts) return formatDate(ts, "h:mm A");
	if (m.creation_browser)
		return new Date(m.creation_browser).toLocaleTimeString([], {
			hour: "numeric",
			minute: "2-digit",
		});
	return "";
}
function msgTimeFull(m) {
	const ts = msgStamp(m);
	if (ts) return exactDate(ts);
	if (m.creation_browser)
		return new Date(m.creation_browser).toLocaleString([], {
			weekday: "short",
			day: "numeric",
			month: "short",
			year: "numeric",
			hour: "numeric",
			minute: "2-digit",
		});
	return "";
}
// Day bucket for a message (timezone-safe via dayLabel), for the "Today /
// Yesterday / 3 July" separators between message groups (UX #23). Empty for a
// dateless streaming placeholder.
function msgDay(m) {
	return dayLabel(msgStamp(m) || (m.creation_browser ? new Date(m.creation_browser) : null));
}
// Precompute a divider label per visible message in ONE pass: a label shows only
// on the first message of a new day. A dateless row (streaming placeholder) is
// skipped without resetting the running day, so it can't split a day group.
const dayDividers = computed(() => {
	const out = [];
	let prev = null;
	for (const m of visibleMessages.value) {
		const d = msgDay(m);
		if (d && d !== prev) {
			out.push(d);
			prev = d;
		} else {
			out.push("");
		}
	}
	return out;
});
// Per-message Copy with a brief "copied" tick, and Edit (load a previous
// command back into the composer to tweak and resend).
const copiedId = ref("");
let _copyTimer = null;
function copyMsg(id, text) {
	copyText(text);
	copiedId.value = id;
	clearTimeout(_copyTimer);
	_copyTimer = setTimeout(() => {
		copiedId.value = "";
	}, 1300);
}
function editCommand(m) {
	input.value = m.content || "";
	nextTick(() => {
		const el = composerRef.value?.el;
		if (el) {
			el.focus();
			const p = input.value.length;
			el.setSelectionRange(p, p);
		}
	});
}
// Cached render payload for an artifact: HTML srcdoc (html/svg) or a base64
// data-url (pdf/image/file). Keyed by `${msgName}::${canvasName}::${theme}` —
// the theme is in the key because the backend themes the srcdoc shell (dark
// preview bg), so a toggle refetches instead of showing the stale scheme.
function cvOf(m, cv) {
	return canvasContent.value[m.name + "::" + cv.name + "::" + (effectiveDark.value ? 1 : 0)];
}
// What to feed the renderer. html/svg need the fetched srcdoc content (sandboxed
// iframe). pdf / image / file render straight from the on-site file_url instead:
// Chrome won't render a base64 data: PDF inline, and a real same-origin URL also
// avoids holding big files as base64 in memory. Every canvas item carries
// file_url (private File on this site, auth-gated by the session cookie).
function cvSrc(m, cv) {
	if (cv.type === "html" || cv.type === "svg") return cvOf(m, cv);
	return cv.file_url || cvOf(m, cv);
}
// Basename (with extension) for a download filename / file card label.
function cvFile(cv) {
	return (cv.name || "file").split("/").pop();
}
// ---- artifact preview side panel (ChatGPT/Claude-style: click a card → slide-
// in panel on the right; PDF/image render directly, xlsx/csv as a table) ----
// { m, cv, url, kind, conv, content?, sheets?, sheetIdx?, text? }
// `conv` is the conversation the artifact was opened FROM. The overlay is
// absolutely positioned inside the ChatView container, so the AppShell's
// conversation sidebar stays clickable behind it: the panel routinely outlives
// the conversation it belongs to, and any action that mixes it with the current
// conversation (the Dashboards hand-off) would act on a mismatched pair.
const artifact = ref(null);
const artifactPanelEl = ref(null);
// Move focus into the panel when it opens so keyboard users land inside it
// and Escape closes it right away (handled in onGlobalKey).
watch(
	() => !!artifact.value,
	(open) => {
		if (open) nextTick(() => artifactPanelEl.value && artifactPanelEl.value.focus());
	}
);
const curSheet = computed(() => {
	const a = artifact.value;
	if (!a || a.kind !== "table" || !a.sheets?.length) return { rows: [] };
	return a.sheets[a.sheetIdx] || { rows: [] };
});
function closeArtifact() {
	artifact.value = null;
}
function setSheet(si) {
	if (artifact.value) artifact.value = { ...artifact.value, sheetIdx: si };
}
async function openArtifact(m, cv) {
	const url = cv.file_url || cvOf(m, cv);
	const t = cv.type;
	const conv = currentId.value;
	if (t === "pdf" || t === "image") {
		artifact.value = { m, cv, url, conv, kind: t };
		return;
	}
	if (t === "html" || t === "svg") {
		let content = cvOf(m, cv);
		if (!content) {
			artifact.value = { m, cv, url, conv, kind: "loading" };
			await ensureCanvas(m);
			content = cvOf(m, cv);
		}
		artifact.value = { m, cv, url, conv, kind: content ? t : "nopreview", content };
		return;
	}
	// "file" (xlsx / csv / txt / …) → ask the backend for a tabular/text preview.
	artifact.value = { m, cv, url, conv, kind: "loading" };
	try {
		const r = await api.previewFile(cv.file_url);
		if (r && r.kind === "table" && Array.isArray(r.sheets) && r.sheets.length) {
			artifact.value = { m, cv, url, conv, kind: "table", sheets: r.sheets, sheetIdx: 0 };
			return;
		}
		if (r && r.kind === "text") {
			artifact.value = { m, cv, url, conv, kind: "text", text: r.text || "" };
			return;
		}
	} catch (e) {
		/* fall through to download-only */
	}
	artifact.value = { m, cv, url, conv, kind: "nopreview" };
}
// ---- "Open in Dashboards" (a Dashboards-builder conversation opened here) ----
// The preview in this thread renders the document but never runs it: main chat's
// canvas has no query-tool bridge. The Dashboards page does, so hand the artifact
// over to it — as the saved dashboard it became, or as this exact canvas message
// for the builder to replay.
function canOpenDash(cv) {
	return canOpenInDashboards({
		originPage: originPage.value,
		originOf: originOf.value,
		conversation: currentId.value,
		cv,
	});
}
async function openInDashboards(m, cv) {
	const conv = currentId.value;
	if (!conv || !canOpenDash(cv)) return;
	let dashboard = null;
	try {
		dashboard = await dashboardForConversation(conv);
	} catch (e) {
		// A lookup that fails (offline, a dashboard since moved out of scope)
		// must never leave a dead button: fall through to the builder promotion,
		// which needs nothing but the transcript.
		dashboard = null;
	}
	closeArtifact();
	// m.creation decides the fork: a conversation keeps building after its first
	// save, so a click on a LATER artifact must promote that artifact, not reopen
	// the older saved row (which would silently answer with a different document).
	// The saved row's name rides along on that fork (`dash`) so the builder can
	// keep editing it rather than starting a second row for the same dashboard.
	router.push(
		dashboardOpenRoute({
			dashboard,
			conversation: conv,
			messageId: m.name,
			messageCreation: m.creation,
		})
	);
}
// Lazily fetch each artifact's render payload (srcdoc content for html/svg, a
// data-url for pdf/image/file) and cache it.
async function ensureCanvas(m) {
	if (!m || !Array.isArray(m.canvas) || !m.canvas.length) return;
	for (const cv of m.canvas) {
		// pdf / image / file render from file_url directly — only html/svg need
		// the fetched srcdoc content.
		if (cv.file_url && cv.type !== "html" && cv.type !== "svg") continue;
		const dark = effectiveDark.value ? 1 : 0;
		const key = m.name + "::" + cv.name + "::" + dark;
		if (canvasContent.value[key]) continue;
		try {
			const r = await api.getCanvas(m.name, cv.name, dark);
			const payload = r && (r.content || r.data_url);
			if (payload) canvasContent.value = { ...canvasContent.value, [key]: payload };
		} catch (e) {
			/* leave it in the loading state; a reload retries */
		}
	}
	nextTick(scrollBottom);
}
function scrollBottom(smooth = false) {
	const el = threadEl.value;
	if (!el) return;
	if (smooth && "scrollTo" in el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
	else el.scrollTop = el.scrollHeight;
}
// Distance in px from the very bottom of the thread. 0 == pinned to newest.
function distanceFromBottom() {
	const el = threadEl.value;
	if (!el) return 0;
	return el.scrollHeight - el.scrollTop - el.clientHeight;
}
// Runs on every user scroll: decide whether we're "at the bottom" (keep pinning
// as new content arrives) and whether to reveal the jump-to-latest arrow.
function onThreadScroll() {
	const d = distanceFromBottom();
	pinnedToBottom.value = d <= 80;
	showScrollDown.value = d > 140;
}
// Arrow click: smooth-scroll to the newest message and re-pin.
function jumpToBottom() {
	pinnedToBottom.value = true;
	showScrollDown.value = false;
	scrollBottom(true);
}
// Keep the thread pinned to the newest message while its content is still
// settling — streaming text, plus images/charts/mermaid that finish loading
// *after* the initial scrollBottom() and would otherwise leave a freshly
// refreshed chat parked mid-thread (the "10 messages, opens at the top" bug).
// A ResizeObserver on the inner content re-pins on every growth, but only while
// the user hasn't deliberately scrolled up.
let threadRO = null;
watch(threadInnerEl, (el) => {
	if (threadRO) {
		threadRO.disconnect();
		threadRO = null;
	}
	if (el && typeof ResizeObserver !== "undefined") {
		threadRO = new ResizeObserver(() => {
			if (pinnedToBottom.value) scrollBottom();
			else onThreadScroll();
		});
		threadRO.observe(el);
	}
});
onBeforeUnmount(() => {
	if (threadRO) {
		threadRO.disconnect();
		threadRO = null;
	}
});
// Auto-grow now lives in <Composer> (it watches modelValue), so every former
// autoGrow() call here is gone — only the caret work around them remains.
function onKey(e) {
	const mn = mention.value;
	if (mn.open && mn.items.length) {
		if (e.key === "ArrowDown") {
			e.preventDefault();
			mention.value = { ...mn, index: (mn.index + 1) % mn.items.length };
			return;
		}
		if (e.key === "ArrowUp") {
			e.preventDefault();
			mention.value = { ...mn, index: (mn.index - 1 + mn.items.length) % mn.items.length };
			return;
		}
		if (e.key === "Enter" || e.key === "Tab") {
			e.preventDefault();
			applyMention(mn.items[mn.index]);
			return;
		}
		if (e.key === "Escape") {
			e.preventDefault();
			mention.value = { ...mn, open: false };
			return;
		}
	}
	// Up/Down: recall sent prompts — only when the caret is at the very start
	// (Up) or end (Down) so it doesn't fight normal multi-line editing.
	const el = e.target;
	if (
		e.key === "ArrowUp" &&
		(input.value === "" || el.selectionStart === 0) &&
		promptHistory.value.length
	) {
		e.preventDefault();
		if (histIdx.value === null) {
			histDraft.value = input.value;
			histIdx.value = promptHistory.value.length;
		}
		if (histIdx.value > 0) {
			histIdx.value -= 1;
			input.value = promptHistory.value[histIdx.value];
			nextTick(() => {
				const p = input.value.length;
				el.setSelectionRange(p, p);
			});
		}
		return;
	}
	if (
		e.key === "ArrowDown" &&
		histIdx.value !== null &&
		el.selectionStart === input.value.length
	) {
		e.preventDefault();
		if (histIdx.value < promptHistory.value.length - 1) {
			histIdx.value += 1;
			input.value = promptHistory.value[histIdx.value];
		} else {
			histIdx.value = null;
			input.value = histDraft.value;
		}
		nextTick(() => {
			const p = input.value.length;
			el.setSelectionRange(p, p);
		});
		return;
	}
	if (e.key === "Enter" && !e.shiftKey) {
		e.preventDefault();
		send();
	}
}

async function loadConversation(id) {
	// One-shot wiki grounding is per-turn: never carry an armed pill into a
	// different conversation (matches how modelOverride/auto-apply reload here).
	groundNextTurn.value = false;
	if (!id) {
		messages.value = [];
		originPage.value = "";
		originOf.value = "";
		modelOverride.value = "";
		thinkingOverride.value = "";
		promptHistory.value = [];
		histIdx.value = null;
		histDraft.value = "";
		return;
	}
	const d = await api.getConversation(id);
	// Stale-response guard: if the user navigated to a different conversation
	// while this request was in flight, drop the result. Without this, a slow
	// get_conversation response clobbers the conversation you actually switched
	// to with the wrong (or empty) messages — and only a page refresh, which
	// does a single clean load, would put it right. (Root cause of "open a
	// chat, switch away and back, it shows empty until I refresh".)
	if (currentId.value !== id) return;
	messages.value = d?.messages || [];
	// VR4-2: re-inject any failed optimistic bubbles whose send was rejected while THIS conversation
	// was off-screen (their rendered copy was dropped when messages was replaced). They are kept
	// origin-scoped and carry the voice-release token, so the resend affordance survives a mid-send
	// switch. Dedupe by name (a rejection that lands while we're on-screen already holds one).
	messages.value = injectPendingBubbles(messages.value, _pendingSends.peek(id));
	// get_conversation returns {conversation: {...}, messages: [...]}. Reading
	// d.model_override (one level too high) silently yielded undefined, so a
	// saved pin always rendered as "Auto" after a reload.
	modelOverride.value = d?.conversation?.model_override || "";
	thinkingOverride.value = d?.conversation?.thinking_override || "";
	// The origin and the conversation it was read for, written as a pair — the
	// gate compares the second against currentId, so neither is ever trusted
	// alone. Nothing above blanks them: a refresh of the conversation already on
	// screen (message:enriched fires one right after every canvas enrichment,
	// onResync on every tab focus) would otherwise pull "Open in Dashboards" out
	// of the DOM for a full RTT at exactly the moment it is most likely to be
	// clicked — and leave it gone for good when that refetch rejects.
	originPage.value = d?.conversation?.origin_page || "";
	originOf.value = id;
	// Per-conversation auto-apply + a fresh confirm-card slate for this chat
	// (issue #186): a pending write from another conversation must not linger.
	convAutoApply.value = !!(d?.conversation && d.conversation.auto_apply);
	autoApplyNote.value = "";
	// Per-conversation confirm-card slate (issue #186): drop any parked cards from
	// OTHER conversations, then re-surface this conversation's still-live parked
	// confirmations (R3 fix for #3 - survives reload / reconnect).
	pendingActions.value = pendingActions.value.filter((pa) => pa.conversation === id);
	resyncPendingConfirmations(id);
	// SUXI-1: rebuild the queued chip from server truth (reload / switch / second
	// tab / reconnect all lose the client-only chip otherwise).
	resyncQueuedTurn(id);
	// Seed Up/Down recall from THIS conversation's past prompts. Without this,
	// promptHistory only held prompts typed in the current page session, so
	// after a reload or when opening an existing chat the arrows did nothing.
	// Strip the trailing "📎 name" attachment marker so recall yields the
	// actual typed text.
	promptHistory.value = (d?.messages || [])
		.filter((m) => m.role === "user" && m.content)
		.map((m) => m.content.replace(/\n*📎[^\n]*$/, "").trim())
		.filter(Boolean);
	histIdx.value = null;
	histDraft.value = "";
	for (const m of messages.value) {
		if (Array.isArray(m.canvas) && m.canvas.length) ensureCanvas(m);
	}
	// Resume the in-progress indicator if the last reply is still streaming, so a
	// refreshed or duplicated tab shows "Thinking…"/streaming (realtime deltas go
	// to every tab) instead of a frozen blank reply. Freshness-guarded so a stale
	// streaming=1 (crashed worker) can't lock the composer forever; live deltas +
	// run:end clear it normally.
	recovering.value = null;
	const _streaming = [...messages.value]
		.reverse()
		.find((m) => m.role === "assistant" && m.streaming);
	let _resumed = false;
	if (_streaming) {
		const fresh =
			_streaming.modified &&
			new Date() - new Date(_streaming.modified.replace(" ", "T")) < 5 * 60 * 1000;
		if (fresh && _streaming.recovering) {
			// Parked for background recovery: show the recovering banner but fully
			// UNLOCK the composer (clear the whole in-flight state we may have
			// resumed into) so a socket-drop-during-recovery resync doesn't rebuild
			// the locked-spinner limbo. The answer lands via the recovery path.
			recovering.value = { message_id: _streaming.name, reason: "interrupted" };
			sending.value = false;
			waiting.value = false;
			statusPhase.value = null;
			activeTools.value = [];
			currentRunId.value = null;
			store.streamingConvId = null;
			_resumed = true;
		} else if (fresh) {
			sending.value = true;
			waiting.value = !(_streaming.content || "").trim();
			store.streamingConvId = id;
			_resumed = true;
		}
	}
	// Reconcile the sidebar streaming dot with the fetched state: if the store
	// still marks THIS conversation as streaming but its messages say otherwise
	// (run ended while we were on another route, or a stale streaming=1 flag),
	// clear it — otherwise the dot pulses forever. A dot on a DIFFERENT
	// conversation is left alone: its live socket deltas keep it honest.
	if (!_resumed && store.streamingConvId === id) store.streamingConvId = null;
	// F3 (defensive resync parity): if this (re)load shows the in-flight reply already
	// settled — no fresh streaming row — but a live run left the streaming-activity block
	// standing (a missed/late run:end, the CDX-12 symptom), collapse the orphan jv-mark +
	// tool-tally container so the live DOM matches exactly what the reload renders. Scoped
	// to the current run/message, so navigating away from a STILL-streaming chat never
	// clears its live state.
	if (!_resumed) tearDownActivityIfSettled();
	// A freshly opened/refreshed chat should land on the newest message and stay
	// pinned there while late content settles; the ResizeObserver takes over.
	pinnedToBottom.value = true;
	showScrollDown.value = false;
	await nextTick();
	scrollBottom();
	processMermaid();
}

// Render any ```mermaid blocks (the agent's inline pie/flow charts) into SVG.
// Lazy-loads mermaid so it never bloats the initial bundle; only runs on
// finalized messages (mid-stream mermaid source is incomplete and would error).
let _mermaid = null;
// Vibrant, high-contrast categorical palette for pie/bar slices — distinct
// hues that stay legible with white section labels in both light and dark.
// (The app's own palette is near-monochrome, which is why the old "neutral"
// mermaid theme rendered charts as washed-out grays.)
const MERMAID_PALETTE = [
	"#6366f1",
	"#06b6d4",
	"#f59e0b",
	"#ec4899",
	"#10b981",
	"#8b5cf6",
	"#ef4444",
	"#14b8a6",
	"#f97316",
	"#3b82f6",
	"#65a30d",
	"#f43f5e",
];
// Guard against overlapping runs: mermaid's render mutates a shared temp DOM
// node, so two concurrent passes can clobber each other and leave a chart as
// raw source. _mmQueued re-runs once if a trigger fires mid-pass.
let _mmRunning = false;
let _mmQueued = false;
async function processMermaid() {
	if (_mmRunning) {
		_mmQueued = true;
		return;
	}
	_mmRunning = true;
	try {
		await _renderMermaid();
	} finally {
		_mmRunning = false;
		if (_mmQueued) {
			_mmQueued = false;
			setTimeout(processMermaid, 0);
		}
	}
}
async function _renderMermaid() {
	const nodes = rootEl.value?.querySelectorAll?.(".jv-mermaid:not([data-rendered])");
	if (!nodes || !nodes.length) return;
	try {
		if (!_mermaid) _mermaid = (await import("mermaid")).default;
	} catch (e) {
		return;
	}
	// Re-initialize each run so the palette tracks the active light/dark theme —
	// mermaid snapshots its theme at init, so a one-time init would freeze it.
	const dark = effectiveDark.value;
	const pie = Object.fromEntries(MERMAID_PALETTE.map((c, i) => [`pie${i + 1}`, c]));
	_mermaid.initialize({
		startOnLoad: false,
		securityLevel: "strict",
		theme: "base",
		fontFamily: "inherit",
		themeVariables: {
			...pie,
			pieStrokeColor: dark ? "#16161a" : "#ffffff",
			pieStrokeWidth: "2px",
			pieOuterStrokeColor: dark ? "#16161a" : "#ffffff",
			pieOuterStrokeWidth: "2px",
			pieSectionTextColor: "#ffffff",
			pieSectionTextSize: "14px",
			pieTitleTextColor: dark ? "#ededf2" : "#171717",
			pieLegendTextColor: dark ? "#b6b6c0" : "#4a4a4f",
			// flow / sequence / line charts, if the agent emits those
			primaryColor: dark ? "#1d1d22" : "#f7f7f8",
			primaryBorderColor: dark ? "#3a3a45" : "#d6e2fb",
			primaryTextColor: dark ? "#ededf2" : "#171717",
			lineColor: dark ? "#5b7cfa" : "#3b82f6",
			textColor: dark ? "#b6b6c0" : "#4a4a4f",
			xyChart: {
				backgroundColor: dark ? "#16161a" : "#ffffff",
				titleColor: dark ? "#ededf2" : "#171717",
				xAxisLabelColor: dark ? "#b6b6c0" : "#4a4a4f",
				xAxisTitleColor: dark ? "#b6b6c0" : "#4a4a4f",
				xAxisTickColor: dark ? "#3a3a45" : "#d6e2fb",
				xAxisLineColor: dark ? "#3a3a45" : "#d6e2fb",
				yAxisLabelColor: dark ? "#b6b6c0" : "#4a4a4f",
				yAxisTitleColor: dark ? "#b6b6c0" : "#4a4a4f",
				yAxisTickColor: dark ? "#3a3a45" : "#d6e2fb",
				yAxisLineColor: dark ? "#3a3a45" : "#d6e2fb",
				plotColorPalette: MERMAID_PALETTE.join(","),
			},
		},
	});
	let n = 0;
	for (const el of nodes) {
		const src = (el.textContent || "").trim();
		if (!src) {
			el.setAttribute("data-rendered", "1");
			continue;
		}
		try {
			const { svg } = await _mermaid.render(
				`jvmmd-${n++}-${Math.floor(performance.now())}`,
				src
			);
			el.innerHTML = svg;
			el.setAttribute("data-rendered", "1"); // mark only AFTER a successful render
			_addChartDownload(el); // hover button to save the chart as PNG
		} catch (e) {
			// Retry transient failures on a later pass; give up (show source) after 3.
			const t = (parseInt(el.getAttribute("data-try") || "0", 10) || 0) + 1;
			if (t >= 3) el.setAttribute("data-rendered", "err");
			else el.setAttribute("data-try", String(t));
		}
	}
	nextTick(scrollBottom);
}
// Drop a hover "download PNG" button onto a rendered chart. The chart is raw
// (markdown-rendered) HTML, not a Vue node, so we wire the button imperatively.
function _addChartDownload(el) {
	if (el.querySelector(".jv-chart-dl")) return;
	const btn = document.createElement("button");
	btn.className = "jv-chart-dl";
	btn.type = "button";
	btn.title = "Download chart as PNG";
	btn.setAttribute("aria-label", "Download chart as PNG");
	btn.innerHTML =
		'<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3"/></svg>';
	btn.addEventListener("click", () => downloadSvgAsPng(el.querySelector("svg")));
	el.appendChild(btn);
}
// Rasterize an inline SVG (the chart) to a 2x PNG and trigger a download. The
// SVG is same-origin inline markup with no external refs, so the canvas isn't
// tainted and toBlob works.
function downloadSvgAsPng(svgEl) {
	if (!svgEl) return;
	const vb = svgEl.viewBox && svgEl.viewBox.baseVal;
	const w = (vb && vb.width) || svgEl.clientWidth || 640;
	const h = (vb && vb.height) || svgEl.clientHeight || 420;
	const xml = new XMLSerializer().serializeToString(svgEl);
	const img = new Image();
	img.onload = () => {
		const scale = 2;
		const canvas = document.createElement("canvas");
		canvas.width = Math.ceil(w * scale);
		canvas.height = Math.ceil(h * scale);
		const ctx = canvas.getContext("2d");
		ctx.fillStyle = effectiveDark.value ? "#16161a" : "#ffffff";
		ctx.fillRect(0, 0, canvas.width, canvas.height);
		ctx.setTransform(scale, 0, 0, scale, 0, 0);
		ctx.drawImage(img, 0, 0, w, h);
		canvas.toBlob((blob) => {
			if (!blob) return;
			const url = URL.createObjectURL(blob);
			const a = document.createElement("a");
			a.href = url;
			a.download = "chart.png";
			document.body.appendChild(a);
			a.click();
			a.remove();
			setTimeout(() => URL.revokeObjectURL(url), 1000);
		}, "image/png");
	};
	img.src = "data:image/svg+xml;base64," + btoa(unescape(encodeURIComponent(xml)));
}
// Clear all per-turn UI state that belongs to the conversation we are leaving,
// so a chat that was mid-stream does not strand the composer when we open
// another: without this, sending stays true (composer stuck in "Stop" mode and
// send() bails on sending) because the leaving run's run:end event is dropped
// by the conversation guard in onEvent.
function resetRunState() {
	sending.value = false;
	waiting.value = false;
	activeTools.value = [];
	currentRunId.value = null;
	store.streamingConvId = null;
	pendingFiles.value = [];
	mention.value = { ...mention.value, open: false };
	histIdx.value = null;
	histDraft.value = "";
}
// Stash the leaving chat's draft and restore the target chat's own, so unsent
// text follows its conversation instead of bleeding into the next one. Both the
// leaving and target scope are canonicalised through _currentScope()/the sentinel,
// so a recovered or typed NEW-chat draft is stashed under (and restored from) the
// stable _NEW_CHAT_SCOPE sentinel instead of being stranded under an unreachable
// null key that swapDraft(null) could never restore (R2-2).
function swapDraft(toId) {
	drafts.value[_currentScope()] = input.value;
	input.value = drafts.value[toId == null ? _NEW_CHAT_SCOPE : toId] || "";
}
async function selectConversation(id) {
	if (id === currentId.value) return;
	swapDraft(id);
	resetRunState();
	// Don't let the macro banner leak across conversations — clear it unless we're
	// navigating into the run's own conversation.
	if (macroRun.value && macroRun.value.conversation !== id) macroRun.value = null;
	currentId.value = id;
	// Selection can also start INSIDE the component (proactive toast, run
	// history) — keep the URL coherent with the sidebar's /c/:id navigation.
	// No-op when the route watcher initiated this call (params already match).
	if (route.params.id !== id) router.replace("/c/" + id);
	await loadConversation(id);
	await nextTick();
	composerRef.value?.focusInput();
}
// Surface a failed action (new chat, send, …) as an error toast. String()-coerces
// the extracted reason so a non-string Frappe error payload can't throw inside the
// caller's catch and re-swallow the very failure we're trying to report.
function notifyActionError(prefix, e) {
	notify(`${prefix} — ${String(_skillErr(e)).replace(/\.$/, "")}. Try again.`, {
		type: "error",
	});
}
// Promote the unsaved new-chat composer to a real conversation id via the shared voiceSendGlue
// helper: migrate the sentinel draft, the active recording take scope (_micConvId), and every
// retained voice recording + its mirror from _NEW_CHAT_SCOPE to `toId` BEFORE any later send or
// navigation. ONE helper for BOTH newChat() and the send-path id adoption so a recording that
// commits late under the sentinel (e.g. a failed one the user retries after the send) is never
// stranded there — a real-scope send/ack would otherwise never match it → guard armed forever,
// recovery routed to the wrong draft (R3-2).
function _promoteNewChatScope(toId) {
	_micConvId = promoteNewChatScope({
		queue: voiceStore,
		drafts: drafts.value,
		fromScope: _NEW_CHAT_SCOPE,
		toId,
		takeScope: _micConvId,
	});
	// VR4-2: a failed bubble that was queued under the sentinel follows the promoted conversation,
	// so returning to the real id (not the transient sentinel) re-injects it with its token intact.
	_pendingSends.reassign(_NEW_CHAT_SCOPE, toId);
}
async function newChat() {
	// Create FIRST, mutate the UI only on success. If the backend 500s, we must
	// not leave the user on a half-reset screen (blank draft + wiped run state
	// but still the old conversation) with no feedback — surface why and bail,
	// keeping them exactly where they were.
	let conv;
	try {
		conv = await api.createOrFocusEmpty();
	} catch (e) {
		notifyActionError("Couldn't start a new chat", e);
		return;
	}
	swapDraft(null);
	resetRunState();
	currentId.value = conv?.name || conv;
	// This conversation IS the unsaved new-chat composer getting its id. The recovered/typed
	// new-chat draft (already restored into `input` by swapDraft above) and its still-retained
	// voice records lived under the _NEW_CHAT_SCOPE sentinel — migrate draft + records + mirror +
	// take scope to the real id via the shared promotion helper so the text follows the conversation
	// and a later send can release the records by the real scope instead of stranding them (R2-2/R3-2).
	if (currentId.value) _promoteNewChatScope(currentId.value);
	messages.value = [];
	// loadConversation is the only other writer and the route watcher no-ops here
	// (currentId already equals this id), so without this the previous chat's
	// origin survives onto a brand-new one — and the first html the agent draws
	// there would offer "Open in Dashboards" over an ordinary chat's artifact.
	originPage.value = "";
	originOf.value = "";
	// VR5-3: _promoteNewChatScope above moved any sentinel-scoped failed bubble (+ its voice-release
	// token) onto the real id, but the route watcher no-ops here (currentId already equals this id),
	// so loadConversation()'s pending-bubble peek never runs — re-inject them now, exactly as
	// loadConversation does, so the failed bubble stays visible + resendable in-session and its leave
	// guard is resolvable, instead of being hidden until some later away-and-back.
	if (currentId.value)
		messages.value = injectPendingBubbles(messages.value, _pendingSends.peek(currentId.value));
	// Reflect the new chat's own id in the URL (/c/:id) so it's refresh-persistent
	// and shareable, instead of dropping to the id-less home. currentId already
	// equals this id, so the route watcher no-ops (no redundant load), and it
	// replaces any /c/:old-id so the previous conversation isn't stranded.
	if (currentId.value) router.replace("/c/" + currentId.value);
	await store.loadConversations();
	// A genuinely-new chat may have just moved the server-side counter onto a
	// multiple of three - re-probe, but never await it (must never block chat).
	probeGreeting();
	await nextTick();
	composerRef.value?.focusInput();
}
// Welcome cards drop the prompt into the input (don't send) so the user can
// tweak it first.
function fillInput(text) {
	input.value = text;
	nextTick(() => {
		composerRef.value?.focusInput();
	});
}
function openErpDesk() {
	window.open("/app", "_blank");
}
async function selectModel(m) {
	modelMenuOpen.value = false;
	const prev = modelOverride.value;
	modelOverride.value = m;
	if (currentId.value) {
		try {
			const res = await api.setConversationModel(currentId.value, m);
			// The server rejects a model the customer has not configured. Roll the
			// pill back rather than showing a pin the next turn will not honour.
			if (res && res.ok === false) modelOverride.value = prev;
		} catch (e) {
			modelOverride.value = prev;
		}
	}
}

async function selectThinking(level) {
	modelMenuOpen.value = false;
	const prev = thinkingOverride.value;
	thinkingOverride.value = level;
	if (currentId.value) {
		try {
			const res = await api.setConversationThinking(currentId.value, level);
			if (res && res.ok === false) thinkingOverride.value = prev;
		} catch (e) {
			thinkingOverride.value = prev;
		}
	}
}
function onDocClick(e) {
	if (!e.target.closest(".jv-modelmenu-wrap")) modelMenuOpen.value = false;
	if (!e.target.closest(".jv-composer")) mention.value = { ...mention.value, open: false };
}
async function retry(messageId) {
	if (retrying.value) return;
	retrying.value = true;
	sending.value = true;
	waiting.value = true;
	runStartMs.value = 0;
	nowMs.value = 0;
	currentMsgId.value = null;
	try {
		const r = await api.retryMessage(messageId);
		if (r && r.ok === false) {
			sending.value = false;
			waiting.value = false;
			// Lapsed sub: raise the persistent banner (as a blocked send does),
			// not a toast that vanishes before they can renew.
			if (r.reason === "subscription_suspended") {
				if (!suspendedNotice.value) suspendedNotice.value = SUSPENDED_FALLBACK;
			} else {
				// e.g. the single-flight guard ("a reply is already in progress").
				notify(r.reason || "Couldn't retry that.", { type: "error" });
			}
		}
	} catch (e) {
		sending.value = false;
		waiting.value = false;
		notifyActionError("Couldn't retry that", e);
	} finally {
		retrying.value = false;
	}
}

function resendFailed(m) {
	// Re-send a message whose POST failed. Guard FIRST so we never drop the
	// bubble when we can't actually resend: bail if a turn or dictation is
	// active, or if there's no plain text (e.g. an attachment-only message,
	// whose file can't be re-attached from the bubble). Then swap the failed
	// bubble for a fresh optimistic one via send().
	if (sending.value || micState.value === "recording" || voiceBusyCount.value > 0) return;
	const txt = (m.content || "").replace(/\n*📎[^\n]*$/, "").trim();
	if (!txt) return;
	messages.value = messages.value.filter((x) => x.name !== m.name);
	// VR4-2: this failed bubble is being REPLACED by a fresh send — drop its origin-scoped pending
	// record so it isn't re-injected as a stale duplicate on return. A new failure records a new one.
	// The bubble is on screen, so its origin is the current scope.
	_pendingSends.remove(_currentScope(), m.name);
	// Carry the ORIGINAL send's voice-release token: the first send failed (so its committed
	// clips were never released), and this resend delivers the SAME text — on success it must
	// release exactly those records (R2-1 inverse: a failed-bubble resend has fromMain=false and
	// otherwise never releases delivered voice audio → a mirror leak + a guard that never clears).
	send(txt, m.voiceAck || null);
}

// One-shot viewing context from a "Discuss in chat" hand-off (chatPrefill's
// optional `context`, e.g. a dashboard): consumed by the first send below.
let _prefillSendContext = null;
async function send(textArg, resendAck) {
	// Don't race a dictation that hasn't landed: sending now would drop the spoken words (the
	// transcript would arrive AFTER the message left the composer). Block on the real busy
	// signal — recording, or a recording still being transcribed — NOT hasUnfinished(), which
	// would also trap the user behind a terminally-failed chip.
	if (micState.value === "recording" || voiceBusyCount.value > 0) {
		notify("Finishing dictation…", { type: "info" });
		return;
	}
	// Capture the composer scope NOW, before a brand-new chat adopts its server id below: on
	// a successful main-composer send its transcribed recordings become durable, so their
	// retained audio + leave guard can be released.
	const _sentScope = _currentScope();
	const fromMain = typeof textArg !== "string";
	const text = (fromMain ? input.value : textArg).trim();
	// PAYLOAD-bound voice release: bind the release to the transcribed recordings whose text is
	// ACTUALLY PRESENT in THIS outgoing payload — captured NOW, before the POST. A recording the
	// user EDITED or DELETED out of the composer is absent from `text`, so captureSentInPayload
	// leaves it OUT of the token and its audio is RETAINED (never acknowledged → never lost). A
	// same-scope recording that commits AFTER this capture is likewise not in the token. A resend
	// reuses the ORIGINAL send's token (it rides on the failed bubble). A programmatic send that
	// leaves the composer intact (no resend token, not fromMain) releases nothing.
	const _voiceAck =
		resendAck ||
		(fromMain && voiceStore ? voiceStore.captureSentInPayload(_sentScope, text) : null);
	// The recordings this message is about to leave behind: terminally failed, in this scope, so
	// their words are not in the payload and never will be. Read BEFORE the POST; flagged only if
	// the server accepts it, so their chips can stop reading like leftover clutter next to an
	// already-answered message.
	const _failedAtSend = fromMain && voiceStore ? voiceStore.failedIdsForScope(_sentScope) : [];
	const attachments = fromMain ? pendingFiles.value.slice() : [];
	if ((!text && !attachments.length) || sending.value) return;
	// canSend already darkens the Send button, but Enter routes here directly (onKey
	// preventDefaults and calls send()), and a programmatic send never sees the button
	// at all. With no model connected the turn can only fail at the agent, so refuse it
	// here and keep the banner's own wording rather than surfacing a backend error.
	if (noAiConnected.value) {
		notify("No AI connected. Connect a model to start chatting again.", { type: "info" });
		return;
	}
	if (text && promptHistory.value[promptHistory.value.length - 1] !== text) {
		promptHistory.value.push(text); // for Up/Down recall
	}
	histIdx.value = null;
	histDraft.value = "";
	if (fromMain) {
		input.value = "";
		pendingFiles.value = [];
		mention.value = { ...mention.value, open: false };
	}
	// A fromMain send clears the composer, so any transcribed recording in this scope whose text
	// the user EDITED OUT (absent from the payload token above) can no longer be released by a
	// payload match — left alone it lingers as a silent record, arming the leave guard forever
	// with no chip. Mark those ACTIONABLE now (Restore/Download/Discard chips render from
	// voiceQ.retained); correct for every send outcome, since the edited-out text is gone from
	// the composer whether the POST then succeeds, is rejected, or throws.
	if (fromMain && voiceStore) voiceStore.markUnsentOrphans(_sentScope, _voiceAck);
	// No awaited pre-flight for a brand-new chat (latency plan, Phase 1.3):
	// the backend's send_message creates/focuses the empty conversation
	// itself and returns conversation_id — two fewer round-trips before the
	// first message even leaves the browser. The sidebar refresh happens
	// after the send resolves, off the critical path.
	sending.value = true;
	waiting.value = true;
	stoppedRunId.value = null;
	runStartMs.value = 0;
	nowMs.value = 0;
	currentMsgId.value = null;
	const isImgAtt = (a) =>
		/\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(a.file_name || a.file_url || "");
	const imgAtts = attachments.filter(isImgAtt);
	const otherAtts = attachments.filter((a) => !isImgAtt(a));
	const marker = otherAtts.length ? "📎 " + otherAtts.map((a) => a.file_name).join(", ") : "";
	const optimistic = [text, marker].filter(Boolean).join("\n\n");
	const optCanvas = imgAtts.map((a, i) => ({
		name: `tmpimg-${Date.now()}-${i}`,
		type: "image",
		file_url: a.file_url,
		title: a.file_name || "image",
	}));
	// creation_browser: local send time so the hover timestamp shows before
	// the server copy (with its site-tz creation) reconciles this tmp row
	const tmpName = `tmp-${Date.now()}`;
	// Hold a stable REFERENCE to the optimistic bubble (VR4-2): a mid-send conversation switch
	// replaces messages.value, so on rejection we mutate/re-inject THIS object rather than a
	// messages.value.find() that returns nothing once the array was swapped by loadConversation().
	const _optBubble = {
		name: tmpName,
		role: "user",
		content: optimistic,
		creation_browser: Date.now(),
		canvas: optCanvas.length ? optCanvas : undefined,
		// Ride the payload's voice-release token on the bubble so, if this POST fails, a
		// resend of this very bubble releases the SAME recordings this send would have (R2-1).
		// Omitted when there is no dictated audio to release.
		voiceAck: _voiceAck && _voiceAck.length ? _voiceAck : undefined,
	};
	messages.value = [...messages.value, _optBubble];
	await nextTick();
	scrollBottom();
	try {
		// The conversation we're sending FROM. The user may switch to another chat
		// while this POST is in flight, so all post-send reconciliation gates on
		// "still on the chat we sent from" — never yank them back to this one.
		const sentFrom = currentId.value || "";
		// One-shot wiki grounding: pass the armed flag but only CONSUME it on a
		// successful send, so a rejected send (and its Retry) keeps grounding armed.
		const groundWiki = groundNextTurn.value;
		const sendCtx = groundWiki ? { ground_wiki: 1 } : _prefillSendContext || undefined;
		_prefillSendContext = null; // one-shot: only the prefill's first send carries it
		const r = await api.sendMessage(sentFrom, text, undefined, attachments, sendCtx);
		if (r && r.ok === false) {
			// The server rejected the send (e.g. the single-flight guard:
			// "a reply is already in progress", or the monthly usage cap).
			// Nothing was persisted — recover it (below) so no work and no voice audio
			// strands, then surface the reason — otherwise the spinner would hang forever
			// (no run:start / run:error is coming).
			// Decide from the STABLE bubble reference (its token), not a messages.value lookup that a
			// mid-send switch already invalidated (VR4-2). `_sentScope` is the ORIGIN conversation.
			const _originOnScreen = _currentScope() === _sentScope;
			const _plan = planRejectedSend({
				fromMain,
				bubbleVoiceAck: _optBubble.voiceAck,
			});
			if (_plan.keepBubble) {
				// A failed-bubble RESEND of transcribed recordings: KEEP its bubble as failed carrying
				// the SAME voiceAck so the user can resend again and eventually release them —
				// dropping it would strand those records behind an armed leave guard with no chip
				// and no action (R3-3). Store it ORIGIN-scoped so it survives a mid-send switch,
				// and show it now if the origin is still on screen (VR4-2).
				_optBubble.failed = true;
				_pendingSends.add(_sentScope, _optBubble);
				if (_originOnScreen && !messages.value.some((x) => x.name === tmpName))
					messages.value = [...messages.value, _optBubble];
			} else {
				// Drop the bubble (a main-composer send / a programmatic send). Restore its text to
				// the ORIGIN composer when it is on screen, else that scope's draft — never bleed it
				// into whatever chat the user switched to, nor lose it on a mid-send switch (VR4-2).
				if (_originOnScreen) {
					messages.value = messages.value.filter((x) => x.name !== tmpName);
					if (_plan.restoreText && !input.value) input.value = text;
				} else if (_plan.restoreText && !drafts.value[_sentScope]) {
					drafts.value[_sentScope] = text;
				}
			}
			sending.value = false;
			waiting.value = false;
			// Lapsed sub: raise the persistent banner (with its Renew link)
			// rather than a toast that vanishes before they can act on it.
			if (r.reason === "subscription_suspended") {
				if (!suspendedNotice.value) suspendedNotice.value = SUSPENDED_FALLBACK;
				return;
			}
			// A rollout started under this tab: the full-page gate only latches at
			// boot, so reload to land on it rather than leave them retrying.
			if (r.reason === "release_update_required") {
				notify(`${agentName} is being updated. Reloading…`, { type: "error" });
				setTimeout(() => window.location.reload(), 1500);
				return;
			}
			if (r.reason === "workspace_resetting") {
				notify(`${agentName} is being reset. Chat will be back in a few minutes.`, {
					type: "warning",
				});
				return;
			}
			if (r.reason === "llm_not_configured") {
				notify("No AI model is connected. Connect one in Settings → AI models.", {
					type: "warning",
				});
				return;
			}
			notify(
				r.reason === "usage_limit"
					? `Monthly usage limit reached. Ask your ${agentName} admin to raise your limit.`
					: r.reason || "Couldn't send your message.",
				{ type: "error" }
			);
			return;
		}
		// Send accepted — the one-shot grounding is now consumed.
		if (groundWiki) groundNextTurn.value = false;
		// The payload's voice-derived text is now durably in the conversation — release EXACTLY
		// the recordings this send captured (audio + leave guard). Payload-bound, so a same-scope
		// recording that committed while this POST was in flight is NOT released, and a
		// failed-bubble resend (carrying the original token) releases its delivered records here
		// too. A programmatic send with no token leaves input.value intact and releases none.
		if (_voiceAck) voiceStore?.acknowledge(_voiceAck);
		// This accepted message went out WITHOUT these recordings' words. Flag them so their
		// chips say so rather than reading like leftover clutter next to an already-answered
		// message, and so their Retry promises the current draft, not an edit of a sent message.
		if (voiceStore) for (const _fid of _failedAtSend) voiceStore.markSentWithout(_fid);
		// Phase-0 admission: the send was accepted but QUEUED (all slots taken).
		// Show the "~N ahead" chip + Cancel instead of the streaming spinner; the
		// reply begins when a slot frees (run:start clears queuedTurn). Position
		// refreshes via the queue:position realtime event + poll-on-focus.
		if (r && r.queued) {
			queuedTurn.value = {
				run_id: r.run_id,
				message_id: r.message_id,
				position: r.queued_position || null,
			};
			waiting.value = false; // not streaming yet — the chip carries the state
		}
		if (r?.conversation_id) {
			const _stillOnSentChat = (currentId.value || "") === sentFrom;
			// VR4-3: promoting the new-chat SENTINEL scope to the server's real id — migrating the
			// queued voice records + their mirror + the stashed draft + the take scope — is
			// VISIBILITY-INDEPENDENT: the server created a real conversation whether or not the user is
			// still looking at it. Run it WHENEVER we sent from the sentinel and got a real id back, so
			// a mid-send chat switch can't leave those clips stranded under the sentinel (where no
			// real-scope send/ack ever reaches them → guard armed forever, a later retry misrouted).
			// Same helper newChat() uses. Only currentId + the URL below stay gated on visibility.
			if (_sentScope === _NEW_CHAT_SCOPE && r.conversation_id !== _NEW_CHAT_SCOPE)
				_promoteNewChatScope(r.conversation_id);
			if (_stillOnSentChat) {
				// Still on the chat we sent from — safe to reconcile it. Adopt the server's id when it
				// differs (a brand-new chat that just got its id, or a stale/reaped conversation
				// send_message fell back to a fresh one for), and keep the URL on it so a refresh
				// doesn't restore a dead /c/:id (route.params.id outranks localStorage on boot). NEVER
				// yank a user who switched away — that is the ONLY part gated on visibility.
				if (r.conversation_id !== currentId.value) {
					currentId.value = r.conversation_id;
					// A send into a conversation the server can no longer find falls back
					// to a FRESH one (chat/api.py), and nothing re-reads the conversation
					// here: loadConversation doesn't run, and the route watcher no-ops
					// because currentId already equals the new id. The origin binding
					// fails the gate closed on its own, but the pair is dropped together
					// on every other swap and this is one — a builder origin left behind
					// on an ordinary chat is the shape this fence exists for.
					originPage.value = "";
					originOf.value = "";
					if (route.params.id !== r.conversation_id)
						router.replace("/c/" + r.conversation_id);
				}
				// Empties are hidden from the sidebar; surface the row now it has a message.
				if (!store.conversations.some((c) => c.name === currentId.value))
					store.loadConversations();
			} else {
				// The user switched conversations mid-send: don't yank them back — just refresh the
				// sidebar so the chat we sent into (now non-empty) surfaces. The scope migration above
				// already ran, so the sentinel-scoped clips followed the conversation regardless.
				store.loadConversations();
			}
		}
	} catch (e) {
		// send_message threw (e.g. a 500). Stop the spinner and mark the bubble as not-sent with an
		// inline Retry, instead of leaving it looking delivered. Keep it (a post-ack timeout may have
		// actually delivered it; a mid-send conversation switch shouldn't strand a draft). Store it
		// ORIGIN-scoped (VR4-2) so it — and its voice token — survive a switch made during the POST,
		// and show it now if the origin is still on screen.
		_optBubble.failed = true;
		_pendingSends.add(_sentScope, _optBubble);
		if (_currentScope() === _sentScope && !messages.value.some((x) => x.name === tmpName))
			messages.value = [...messages.value, _optBubble];
		sending.value = false;
		waiting.value = false;
		notifyActionError("Couldn't send your message", e);
	}
}

// Proactive (Jarvis-initiated) conversation toast.
const proactiveToast = ref(null);
function openProactive() {
	const t = proactiveToast.value;
	if (!t) return;
	swapDraft(t.id);
	resetRunState();
	currentId.value = t.id;
	loadConversation(t.id);
	proactiveToast.value = null;
}
function onEvent(p) {
	// Auto-generated title arrives async (worker titles the chat after the
	// first real turn). Handle it before the current-conversation guard so the
	// sidebar updates even if the user has since switched chats.
	if (p.kind === "conversation:renamed" && p.conversation_id) {
		store.applyRemoteRename(p.conversation_id, p.title);
		return;
	}
	// Jarvis started a conversation with us (proactive). Refresh the sidebar and
	// surface a toast; handled before the current-conversation guard.
	if (p.kind === "conversation:new" && p.conversation_id) {
		store.applyRemoteNew();
		proactiveToast.value = {
			id: p.conversation_id,
			title: p.title || `Message from ${agentName}`,
			preview: p.preview || "",
		};
		return;
	}
	// Macro-run events use `conversation` (not conversation_id) and are handled
	// before the current-conversation guard so the banner tracks the run.
	if (p.kind === "macro:progress") {
		if (p.conversation === currentId.value) {
			if (_macroDoneTimer) {
				clearTimeout(_macroDoneTimer);
				_macroDoneTimer = null;
			}
			macroRun.value = {
				run: p.macro_run,
				conversation: p.conversation,
				step: p.step || 0,
				total: p.total || (macroRun.value && macroRun.value.total) || 0,
				label: p.label || "",
				status: "running",
			};
		}
		patchMacroRunRow(p, false); // live-advance the open run-history dashboard
		return;
	}
	if (p.kind === "macro:done") {
		if (p.conversation === currentId.value && macroRun.value) {
			macroRun.value = { ...macroRun.value, status: p.status || "completed" };
			if (_macroDoneTimer) clearTimeout(_macroDoneTimer);
			_macroDoneTimer = setTimeout(() => {
				if (macroRun.value && macroRun.value.conversation === p.conversation)
					macroRun.value = null;
				_macroDoneTimer = null;
			}, 4000);
		}
		patchMacroRunRow(p, true);
		return;
	}
	// A gated ERP write was parked awaiting the owner's Confirm click (issue
	// #186). Keyed by `conversation` (like macro events), so handle it before the
	// conversation_id guard. Only surface it in the conversation on screen; an
	// off-conversation pending write is ignored here (the card is realtime-only).
	if (p.kind === "action:pending") {
		// #10: a write parked by a run the user already Stopped gets no card. This
		// branch returns above the shared stoppedRunId guard below, so it must make
		// the same check itself.
		if (p.run_id && p.run_id === stoppedRunId.value) return;
		// #2: the server can publish conversation="" when it cannot resolve one.
		// The event still reached THIS user's socket about THIS active turn, so
		// attach it to the current conversation rather than dropping the card.
		const conv = p.conversation || currentId.value;
		if (conv === currentId.value) {
			// #4: append (deduped by token) so a second parked write in the same turn
			// gets its OWN card instead of overwriting the first still-valid one.
			enqueuePending({
				conversation: conv,
				token: p.token,
				tool: p.tool,
				summary: p.summary || "",
				preview: p.preview || null,
				run_id: p.run_id || null,
				expires_at: p.expires_at || null,
			});
		}
		return;
	}
	if (p.conversation_id !== currentId.value) return;
	if (p.run_id && p.run_id === stoppedRunId.value) return; // user stopped this run
	if (p.message_id && stoppedMsgIds.value.has(p.message_id)) return; // …incl. a later "recovered" run for a stopped reply
	switch (p.kind) {
		case "run:recovering":
			// A managed turn was parked for background recovery (a connection
			// hiccup, or openclaw auto-compacting on context overflow). Don't trap
			// the user behind a locked spinner: unlock the composer and show a
			// distinct "still working" banner. The answer lands later via the
			// recovery path (assistant:delta + run:end, run_id "recovered").
			// CDX-3: a stale-epoch (or post-terminal) recovering banner is ignored.
			if (pumpFenceReject(p)) break;
			pumpFenceAccept(p, false);
			recovering.value = { message_id: p.message_id, reason: p.reason || "interrupted" };
			waiting.value = false;
			sending.value = false;
			statusPhase.value = null;
			activeTools.value = [];
			currentRunId.value = null;
			store.streamingConvId = null;
			break;
		case "run:status":
			// Lightweight progress signal (e.g. waking a cold container) between
			// run:start and the first token — keeps the connect window honest.
			if (p.status === "waking") statusPhase.value = "waking";
			break;
		case "run:start":
			// CDX-3: a stale-epoch run:start (a pump that lost the lease, or one that
			// arrives after a higher-epoch terminal) must not re-lock a completed reply.
			if (pumpFenceReject(p)) break;
			pumpFenceAccept(p, false);
			currentRunId.value = p.run_id;
			currentMsgId.value = p.message_id;
			recovering.value = null;
			// Phase-0 admission: a queued turn just got promoted and is now
			// streaming — retire the "~N ahead" chip.
			if (queuedTurn.value && queuedTurn.value.run_id === p.run_id) queuedTurn.value = null;
			runStartMs.value = Date.now();
			nowMs.value = Date.now();
			activeTools.value = [];
			waiting.value = true;
			statusPhase.value = "model";
			store.streamingConvId = p.conversation_id || currentId.value;
			break;
		case "queue:position":
			// Phase-0 admission: this queued turn's approximate position shifted
			// (someone ahead settled / cancelled). Update the chip.
			if (queuedTurn.value && queuedTurn.value.run_id === p.run_id)
				queuedTurn.value = { ...queuedTurn.value, position: p.position };
			break;
		case "turn:cancelled":
			// A queued turn was cancelled (by this user, or system age-out).
			if (queuedTurn.value && queuedTurn.value.run_id === p.run_id) {
				queuedTurn.value = null;
				sending.value = false;
				waiting.value = false;
				// SUXI-3: surface WHY the message disappeared (system age-out reason,
				// or the cancel reason) instead of a silently emptied composer. Routed
				// through the state->copy table (SUXI-7). Guarded by the chip check so
				// the tab that itself clicked Cancel (chip already cleared) doesn't
				// double-toast on top of its own optimistic "Cancelled.".
				notify(p.reason || TURN_STATE_COPY.cancelled(), { type: "info" });
			}
			break;
		case "action:confirmed":
			// SUX-3: a confirm write was saved. Acknowledge immediately so the
			// card doesn't vanish into silence while the continuation turn queues.
			confirmedAck.value = true;
			if (_confirmedAckTimer) clearTimeout(_confirmedAckTimer);
			_confirmedAckTimer = setTimeout(() => {
				confirmedAck.value = false;
			}, 3500);
			break;
		case "assistant:delta": {
			// CDX-3 end-to-end fence: skip a superseded writer's frame (lower epoch),
			// a replayed/out-of-order frame (equal epoch, seq <= the last applied), or
			// a straggler after a higher-epoch terminal. Legacy deltas (no pump_epoch)
			// bypass and are always applied, unchanged.
			if (pumpFenceReject(p)) break;
			pumpFenceAccept(p, false);
			waiting.value = false;
			statusPhase.value = null;
			recovering.value = null;
			// Upsert: the message may not be loaded yet when the first delta
			// arrives — add it so streaming text shows immediately (the bug fix).
			let m = messages.value.find((x) => x.name === p.message_id);
			if (!m) {
				m = { name: p.message_id, role: "assistant", content: "", streaming: true };
				messages.value = [...messages.value, m];
			}
			m.content = p.text;
			m.streaming = true;
			nextTick(scrollBottom);
			break;
		}
		case "tool:start": {
			if (pumpFenceReject(p)) break; // CDX-3 (epoch-less legacy tool events bypass)
			pumpFenceAccept(p, false);
			const id = p.tool_call_id || `${p.tool_name}-${activeTools.value.length}`;
			activeTools.value = [
				...activeTools.value,
				{ id, name: p.tool_name, title: p.tool_title || "", status: "running" },
			];
			waiting.value = false;
			statusPhase.value = null;
			nextTick(scrollBottom);
			break;
		}
		case "tool:end": {
			if (pumpFenceReject(p)) break; // CDX-3 (epoch-less legacy tool events bypass)
			pumpFenceAccept(p, false);
			const t = activeTools.value.find((x) => x.id === p.tool_call_id);
			if (t) t.status = p.status || "completed";
			// No tool running anymore and no text yet → the model is reading
			// the results; say so instead of a generic "Thinking…".
			if (!activeTools.value.some((x) => x.status === "running"))
				statusPhase.value = "analyzing";
			break;
		}
		case "canvas": {
			// Agent produced a chart/canvas this turn — attach + render inline.
			const cm = messages.value.find((x) => x.name === p.message_id);
			if (cm) {
				cm.canvas = p.items;
				ensureCanvas(cm);
			}
			break;
		}
		case "run:end": {
			// CDX-3/CDX-12: fence a stale terminal (a superseded writer's late run:end) —
			// it must not clear a fresher run's spinner — AND dedupe a repeat equal-epoch
			// terminal (the finalize backstop re-publish) one-shot so the announcement +
			// reload below fire exactly once per run/epoch. Accept marks this run terminated
			// at pump_epoch E so ANY later lower-epoch straggler is blocked PERMANENTLY.
			if (pumpFenceReject(p, true)) break;
			pumpFenceAccept(p, true);
			// Defensive: if a promoted turn's run:start was missed, retire the chip.
			if (queuedTurn.value && queuedTurn.value.run_id === p.run_id) queuedTurn.value = null;
			const m = messages.value.find((x) => x.name === p.message_id);
			if (m) m.streaming = false;
			// SUX-6: the terminal final text is the last cumulative mirror in the normal
			// case, so a re-render would be identical — skip the visible churn. A VISIBLE
			// replacement is legitimate only when the answer actually changed via snapshot
			// recovery (was_recovered), which the reload below applies.
			if (p.was_recovered) announceSR("Your answer is ready.");
			// SUX-7: enrichment (canvas/attachments/title) may still be running after the
			// authoritative terminal. Keep a subtle "finishing…" affordance until the
			// message:enriched event clears it (a late pop-in is signalled, not silent).
			if (p.message_id) {
				if (p.enrichment_pending)
					enrichmentPending.value = new Set(enrichmentPending.value).add(p.message_id);
				// NB: the CDX-3 fence entry is deliberately NOT cleared here — the
				// terminated-epoch marker must persist to permanently block a later
				// lower-epoch straggler (clearing it re-opened the stale-delta window).
			}
			// Stamp metrics keyed by message_id so they survive the reload below.
			if (p.message_id) {
				runMeta.value = {
					...runMeta.value,
					[p.message_id]: {
						ms: runStartMs.value ? Date.now() - runStartMs.value : 0,
						tools: activeTools.value.length,
						names: activeTools.value.map((t) => t.name),
					},
				};
			}
			waiting.value = false;
			sending.value = false;
			statusPhase.value = null;
			activeTools.value = [];
			currentRunId.value = null;
			store.streamingConvId = null;
			// (browser notification moved to the app-scoped global notifier —
			// AppShell attaches it, so it fires on every route, not just here)
			recovering.value = null;
			announceSR(`${agentName} replied.`);
			store.loadConversations();
			// SUX-6 identical-skip (OARF-7): the streamed deltas already painted the
			// final cumulative text, so on the normal path a full reload would just
			// re-render an IDENTICAL message (a visible flash). Do the disruptive
			// reload ONLY when the answer actually changed via snapshot recovery
			// (was_recovered — a legitimate visible replacement), OR when there is no
			// enrichment follow-up to reconcile a non-streamed terminal (e.g. a
			// stopped turn: !enrichment_pending). On the ordinary success path the
			// message:enriched event reloads once enrichment lands — no churn here.
			if (p.was_recovered || !p.enrichment_pending) {
				loadConversation(currentId.value);
				// Re-render charts after the reload settles — late re-renders can swap a
				// freshly-rendered mermaid node back to raw source; these idle passes
				// (mutex-guarded, no-op when nothing's pending) catch that race.
				setTimeout(processMermaid, 300);
				setTimeout(processMermaid, 900);
			}
			break;
		}
		case "message:enriched": {
			// SUX-7: the Relay-Pump finalize job finished the owed enrichment for a
			// settled reply — clear the "finishing…" affordance and pull the late
			// attachments/canvas/title in with one reload.
			if (p.message_id && enrichmentPending.value.has(p.message_id)) {
				const next = new Set(enrichmentPending.value);
				next.delete(p.message_id);
				enrichmentPending.value = next;
			}
			loadConversation(currentId.value);
			setTimeout(processMermaid, 300);
			break;
		}
		case "wiki:nudge": {
			// Post-turn "remember this?" prompt. Don't clobber a card the user is
			// already recording into / editing — only replace an idle (or absent)
			// one, or a card stranded on ANOTHER conversation than the one on
			// screen (invisible, so its stuck recorder would otherwise block every
			// future nudge; cancel it before taking the slot).
			const stale =
				nudge.value &&
				nudge.value.conversationId !== p.conversation_id &&
				nudge.value.conversationId !== currentId.value;
			if (stale && (nudge.value.mode === "recording" || nudge.value.mode === "transcribing"))
				nudgeRec.cancel();
			if (!nudge.value || nudge.value.mode === "idle" || stale) {
				nudge.value = {
					conversationId: p.conversation_id,
					entities: Array.isArray(p.entities) ? p.entities : [],
					mode: "idle",
					text: "",
					saving: false,
				};
			}
			break;
		}
		case "run:error":
			// CDX-3/CDX-12: a terminal — fence a superseded writer's late error, dedupe a
			// repeat equal-epoch terminal one-shot, and mark this run terminated at
			// pump_epoch E (blocks later lower-epoch stragglers).
			if (pumpFenceReject(p, true)) break;
			pumpFenceAccept(p, true);
			if (queuedTurn.value && queuedTurn.value.run_id === p.run_id) queuedTurn.value = null;
			if (p.message_id) {
				errorMeta.value = {
					...errorMeta.value,
					[p.message_id]: { code: p.code || "", changed_data: p.changed_data },
				};
			}
			recovering.value = null;
			waiting.value = false;
			sending.value = false;
			statusPhase.value = null;
			activeTools.value = [];
			currentRunId.value = null;
			store.streamingConvId = null;
			announceSR("That didn't go through. See the error in the chat.");
			loadConversation(currentId.value);
			break;
	}
}

function stopRun() {
	// Actually abort the run (openclaw chat.abort via stop_run) - best-effort:
	// if the abort can't be delivered, the turn finishes server-side and the UI
	// stop still stands. We also mute this run's events + pin the reply so a
	// later recovery can't overwrite the stopped state.
	const cid = currentId.value;
	const rid = currentRunId.value;
	if (currentRunId.value) stoppedRunId.value = currentRunId.value;
	if (currentMsgId.value) stoppedMsgIds.value.add(currentMsgId.value);
	const m = [...messages.value].reverse().find((x) => x.role === "assistant" && x.streaming);
	if (m) {
		m.streaming = false;
		if (m.name) stoppedMsgIds.value.add(m.name);
		// A stop is a state of the turn, not prose: leave whatever streamed
		// alone and flag the row. The marker renders from `stopped`, so a
		// mid-sentence stop no longer reads as a complete answer. The server
		// sets the same flag, so the marker survives a reload.
		m.stopped = true;
	}
	waiting.value = false;
	sending.value = false;
	activeTools.value = [];
	store.streamingConvId = null;
	recovering.value = null;
	if (cid) api.stopRun(cid, rid).catch(() => {});
	notify("Stopped.");
}

// Cancel a pre-dispatch turn (queued OR the pump's preparing/ready window). CDX-8:
// the server ROUTES BY STATE (queued -> cancel_queued clearing the reserved credit;
// preparing/ready -> cancel_preparing_or_ready + placeholder cleanup) and returns
// which path won. The UI KEEPS the chip + composer lock until the server CONFIRMS —
// NO optimistic clear, so a failed/errored cancel never removes the chip while work
// continues invisibly (the reserved-credit leak the old optimistic clear masked).
async function cancelQueued() {
	const q = queuedTurn.value;
	if (!q) return;
	try {
		const r = await api.cancelQueuedTurn(q.run_id);
		if (r && r.ok) {
			// Confirmed cancelled (queued or preparing/ready path). Retire the chip +
			// unlock the composer, and let turn:cancelled reconcile other tabs.
			if (queuedTurn.value && queuedTurn.value.run_id === q.run_id) queuedTurn.value = null;
			sending.value = false;
			waiting.value = false;
			// SUXI-7: route the toast through the state->copy table.
			notify(`${TURN_STATE_COPY.cancelled()}.`);
		} else {
			// It already started (a slot freed the instant we clicked). Drop the stale
			// chip but KEEP the composer locked for the now-streaming reply (run:end
			// resets it) — otherwise the composer would enable during a live turn.
			if (queuedTurn.value && queuedTurn.value.run_id === q.run_id) queuedTurn.value = null;
			sending.value = true;
			waiting.value = true;
			notify((r && r.reason) || "That reply already started.", { type: "info" });
		}
	} catch (e) {
		// Server state UNKNOWN (network/500): KEEP the chip + lock so the user can retry
		// the cancel. No optimistic clear on failure (CDX-8).
		notifyActionError("Couldn't cancel the queued message", e);
	}
}

// Poll-on-focus fallback (SUX-2): when the tab regains focus, refresh the queued
// chip's position in case a realtime queue:position push was missed.
async function refreshQueuePositionOnFocus() {
	const q = queuedTurn.value;
	if (!q) return;
	try {
		const r = await api.getQueuePosition(q.run_id);
		if (!r || r.ok === false) return;
		if (r.state !== "queued") {
			// Promoted or settled while we were away — the stream/end events will
			// reconcile; drop the stale chip.
			if (queuedTurn.value && queuedTurn.value.run_id === q.run_id) queuedTurn.value = null;
		} else if (queuedTurn.value && queuedTurn.value.run_id === q.run_id) {
			queuedTurn.value = { ...queuedTurn.value, position: r.position };
		}
	} catch {
		/* best-effort */
	}
}

// ---- voice dictation (composer mic) — ONE RECORDING, ONE TRANSCRIPTION ----
// Hidden unless get_chat_ui_settings reports stt_enabled AND the browser has MediaRecorder.
// A dictation is ONE MediaRecorder session in timeslice mode (useDictationRecorder): ~15 s
// fragments are mirrored to IndexedDB as they arrive so a crash costs at most the last one,
// and at Stop the fragments are concatenated — that IS the recording, no re-encode — and
// transcribed in a SINGLE call with its full context.
//
// The per-clip design this replaces transcribed each ~15 s slice on its own, which is what
// let the model invent words: a fragment with no beginning and no end gets bridged with
// plausible text, and a pause came back as a phrase nobody said. There are no clips, no gap
// placeholders and no per-clip chips any more — a recording either transcribes or it stays
// here, whole, as ONE actionable chip.
//
// AUDIO IS NEVER LOST: fragments are mirrored before transcription, a transcribed recording's
// audio is retained until its words ride out in an ACCEPTED send, a failed recording keeps its
// Retry/Download/Discard chip, and anything left by a crashed session is re-offered by the
// recovery banner on the next visit.
// SECURITY: transcript text is only ever written into the composer — it is never fed back into
// a transcribe call or prompt (no rolling-context path, by design).
const micState = ref("idle"); // 'idle' | 'recording' (transcription runs in the background)
const _emptyVoiceSnap = {
	capturing: 0,
	transcribing: [],
	failed: [],
	retained: [],
	unpersisted: [],
	total: 0,
	hasUnfinished: false,
};
const voiceQ = ref({ ..._emptyVoiceSnap });
const recoveryTakes = ref([]); // recordings a prior session left un-transcribed, offered on mount
// A STABLE draft-scope id for the unsaved "new chat" composer (which has no server id yet).
// Recordings made there are tagged with THIS instead of a bare null, so recovery routes them
// back to the new-chat draft — never dumped into whatever real conversation happens to be open.
// A missing conversationId must NEVER be read as "the current conversation" during recovery.
const _NEW_CHAT_SCOPE = "__jarvis_new_chat__";
// The scope of the composer currently on screen: a real conversation id, or the new-chat draft
// scope when nothing is saved yet.
const _currentScope = () => currentId.value || _NEW_CHAT_SCOPE;
// Failed optimistic bubbles whose send was rejected, kept ORIGIN-scoped and DECOUPLED from the
// rendered `messages` array so they (and the voice-release token they carry) survive a mid-send
// conversation switch — loadConversation() re-injects a scope's entries when the user returns.
const _pendingSends = createPendingSends();
let _micConvId = null; // scope the take was started in — its transcript belongs to that scope
let voiceStore = null; // createVoiceDictationStore — one per browser session
let voiceMirror = null; // createVoiceAudioMirror — IndexedDB, one per session
let _voiceSessionId = null;
let _activeRecordingId = null; // the take currently being captured, if any

function _fmtClock(s) {
	return Math.floor(s / 60) + ":" + String(Math.max(0, s) % 60).padStart(2, "0");
}
const micClock = computed(() => _fmtClock(micRec.durationS || 0));
// Template-facing M:SS for a recording's length (chips, pills, the recovery banner).
// Deliberately M:SS, not fmtDuration's "2m 14s": it reads as a clock, matching the live mic clock.
const recLength = (s) => _fmtClock(s || 0);
// A recording hasn't landed in the composer yet — still being captured (the stop→assemble window,
// where micState has already flipped to idle) or still being transcribed. Drives the send-time
// block, so a message can never leave the composer before the words it is waiting for land in it.
const voiceBusyCount = computed(
	() => voiceQ.value.transcribing.length + (voiceQ.value.capturing || 0)
);
// WHY the composer is holding a send, in the user's words — surfaced on the Send button itself
// (see canSend) instead of only as a toast that fades in 3.2 s. A control whose enabled state
// lies about what it will do reads as broken, which is the one thing this feature's copy is
// careful about everywhere else.
const voiceSendBlockReason = computed(() => {
	if (micState.value === "recording")
		return "Stop the dictation first — its words are still coming.";
	if (voiceBusyCount.value > 0)
		return "Waiting for the dictation to land in the box (or cancel it below).";
	return "";
});
// A slow transcription and a stalled one look identical while the pill states only the
// RECORDING's length, which never changes. Past this many seconds it also reports how long it
// has actually been waiting — a number it genuinely knows, unlike a percentage.
const VOICE_WAIT_VISIBLE_AFTER_S = 10;
const _voiceNowMs = ref(0);
let _voiceWaitTimer = null;
watch(
	() => voiceQ.value.transcribing.length,
	(n) => {
		if (n > 0 && !_voiceWaitTimer) {
			_voiceNowMs.value = Date.now();
			_voiceWaitTimer = setInterval(() => (_voiceNowMs.value = Date.now()), 1000);
		} else if (!n && _voiceWaitTimer) {
			clearInterval(_voiceWaitTimer);
			_voiceWaitTimer = null;
		}
	}
);
const transcribingWait = (t) => {
	if (!t.since || !_voiceNowMs.value) return "";
	const s = Math.floor((_voiceNowMs.value - t.since) / 1000);
	return s >= VOICE_WAIT_VISIBLE_AFTER_S ? ` · waiting ${s}s` : "";
};

function _voiceSnap() {
	voiceQ.value = voiceStore ? voiceStore.snapshot() : { ..._emptyVoiceSnap };
}

function _joinAppend(prev, t) {
	return prev.trim() ? prev.replace(/\s+$/, "") + " " + t : t;
}

// Apply `mutate(oldText) -> newText` to the composer target for `forId` (the conversation the
// recording was SPOKEN in): the live input when that chat is on screen, else its stashed draft
// (restored by swapDraft on return) — never whatever chat merely happens to be open. On the
// on-screen composer NEVER steal focus back in from elsewhere, and preserve the user's caret —
// follow the growing end only if the caret was already there (and unchanged), otherwise keep an
// in-progress mid-string edit put.
function _mutateComposerFor(forId, mutate) {
	// A missing scope resolves to the new-chat draft, NEVER "the current conversation" — that
	// fallback was the null-conv misroute. Write to the live input only when the recording's
	// scope IS the on-screen composer's scope; otherwise stash into that scope's draft.
	const scope = forId == null ? _NEW_CHAT_SCOPE : forId;
	if (scope === _currentScope()) {
		const el = composerRef.value?.el;
		const focused = !!el && document.activeElement === el;
		const atEnd = el
			? el.selectionStart === el.value.length && el.selectionEnd === el.value.length
			: true;
		const selStart = el ? el.selectionStart : 0;
		const selEnd = el ? el.selectionEnd : 0;
		const next = mutate(input.value);
		if (next === input.value) return;
		input.value = next;
		nextTick(() => {
			// auto-grow now lives in <Composer> (it watches modelValue); only the caret work remains
			if (!focused || !el) return; // do NOT pull focus in when the user is elsewhere
			try {
				if (atEnd) el.selectionStart = el.selectionEnd = el.value.length;
				else {
					el.selectionStart = selStart;
					el.selectionEnd = selEnd;
				}
			} catch (e) {
				/* selection not settable — ignore */
			}
		});
	} else {
		drafts.value[scope] = mutate(drafts.value[scope] || "");
	}
}
const _recScope = (rec) =>
	rec && rec.conversationId != null ? rec.conversationId : _NEW_CHAT_SCOPE;

// A transcript, appended to the draft of the chat it was spoken in.
function _appendTranscript(text, rec) {
	const t = (text || "").trim();
	if (!t) return;
	_mutateComposerFor(_recScope(rec), (prev) => _joinAppend(prev, t));
}

function _ensureVoiceSession() {
	if (voiceStore) return;
	_voiceSessionId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
	// Stamp the current Frappe user so reload recovery is scoped to THIS user only
	// (IndexedDB is per-origin, not per-login — no cross-account audio exposure).
	voiceMirror = createVoiceAudioMirror(_voiceSessionId, session.user);
	voiceStore = createVoiceDictationStore({
		// `signal` (from the store's own AbortController) aborts an in-flight upload on
		// dispose() — no wasted fetch after unmount.
		//
		// transcribeAudio resolves the RESPONSE OBJECT {ok,text,stt_ms,model} — NOT a bare
		// string. The store's `transcribe` contract is Promise<string>; returning the object
		// would let it be String()-coerced to "[object Object]" and committed behind deleted
		// audio. Await it, validate the shape, and hand back ONLY res.text; any malformed or
		// failed payload THROWS so the recording is retried, then RETAINED (never-lose-audio).
		//
		// BUDGET ARITHMETIC (re-derived; the previous version of this comment did not survive
		// review). The client budget has to cover the UPLOAD as well as the server, because
		// `requests`' timeout bounds connect+read only — sending the body is outside it:
		//   * upload — the recorder now encodes at 32 kbps (useDictationRecorder's
		//     AUDIO_BITS_PER_SECOND), so the 300 s worst-case take is ~1.2 MB, ~10 s on a
		//     1 Mbps uplink (it was ~4.6 MB / ~37 s at Chrome's measured 129 kbps default).
		//   * server — ONE transcription attempt now (voice.py `_TRANSCRIBE_ATTEMPTS`), so
		//     10 s connect + 60 s read = 70 s, not 140. whisper-turbo does 300 s of audio in
		//     1.2 s; the second attempt doubled the worst case to buy almost nothing.
		//   * 10 + 70 = ~80 s worst case, against a 150 s client budget — real margin, where
		//     before it was 177 s of worst case against 150 s and the client aborted calls the
		//     server was about to finish, then re-uploaded them.
		// Aborting early is the expensive mistake: it turns a slow-but-fine transcription into a
		// failure and pays OpenRouter for work the user never sees.
		transcribe: async (rec, signal) => {
			const res = await voice.transcribeAudio(rec.blob, {
				durationS: rec.durationS,
				timeoutMs: 150000,
				signal,
			});
			if (res && res.ok === true && typeof res.text === "string") return res.text;
			throw new Error((res && res.error) || "Transcription returned no usable text.");
		},
		mirror: voiceMirror,
		maxAttempts: 2, // the client abort lives in transcribeAudio; the store retries once
		// …and pauses first. An instant re-upload into a 429 or a 5xx is two back-to-back
		// multi-megabyte POSTs for the same answer, and the failing attempt has usually just
		// burned its own timeout — 3 s costs the user nothing they can perceive.
		retryBackoffMs: 3000,
		// A transcript lives only in a volatile composer draft until the message is SENT —
		// retain the audio (and the leave guard) until acknowledge()/discard so a reload can
		// still recover it.
		retainUntilSent: true,
		onCommit: (id, text, rec, resurrected) => _onVoiceCommit(id, text, rec, resurrected),
		// The audio could NOT be confirmed durably mirrored (quota / private mode / tx failure /
		// unavailable IndexedDB). STOP recording rather than keep capturing audio we can't
		// crash-protect, and surface the recording's Download/Retry chip.
		onPersistFail: () => _onVoicePersistFail(),
		onChange: _voiceSnap,
	});
}

// A recording's transcript is ready. `resurrected` means the draft has moved on since this
// recording failed (a later take's words are already in it, or the message went out without
// this one), so the text lands at the END rather than where it was spoken — say so, instead of
// dropping words the user can't place into the box.
function _onVoiceCommit(id, text, rec, resurrected) {
	_appendTranscript(text, rec);
	if (resurrected && (text || "").trim()) {
		notify(
			`Your ${_fmtClock(rec.durationS || 0)} recording was added to the end of your draft.`,
			{
				type: "info",
			}
		);
	}
}

// Recording must not silently continue once the audio can't be durably saved — a crash or reload
// would lose it with no warning. Stop the recorder (what is already captured stays in memory and
// Download-able) and say why.
function _onVoicePersistFail() {
	if (micState.value !== "recording") return;
	void stopMic();
	notify(
		"Couldn't safely save your voice audio on this device (storage may be full or private mode). Recording stopped — download the recording to keep it, or retry once you've freed space.",
		{ type: "error" }
	);
}

const micRec = useDictationRecorder({
	// A ~15 s timeslice fragment arrived mid-take: mirror it NOW so a crash costs at most this
	// much. Fragments are crash-safety only — they are never transcribed on their own.
	onFragment: (frag) => {
		if (voiceStore && _activeRecordingId != null)
			voiceStore.addFragment(_activeRecordingId, frag);
	},
	// Stop: the assembled take goes for ONE transcription with its full context.
	onDone: (take) => {
		const id = _activeRecordingId;
		_activeRecordingId = null;
		if (!voiceStore || id == null) return;
		// WHOLE-TAKE SILENCE GATE (voiceSilenceGate.js). whisper hallucinates confident phrases
		// ("Thank you.") onto silent audio and the provider exposes no no_speech_prob to filter
		// on, so a take the recorder MEASURED as inaudible from end to end is never uploaded. It
		// is NOT deleted: finishSilent() routes it to the same terminal `failed` state an empty
		// transcript produces, so the audio survives behind the chip's Transcribe anyway /
		// Download / Discard. A false positive here is plausible — a muted-then-forgotten mic, a
		// Bluetooth gain quirk, an analyser the OS starves — and it would otherwise destroy up to
		// five minutes of real speech behind a 3-second toast, against this feature's own first
		// invariant. A take with NO measurement (no WebAudio / suspended context) is always
		// transcribed, and one audible word anywhere in the take clears the gate.
		if (isNearSilent(take.peakRms)) {
			voiceStore.finishSilent(id, take);
			notify("Nothing was heard — try closer to the microphone.", { type: "info" });
			return;
		}
		voiceStore.finish(id, take);
	},
	// The take was abandoned (Cancel, or nothing was captured): drop it and its mirrored fragments.
	onCancel: () => {
		const id = _activeRecordingId;
		_activeRecordingId = null;
		if (voiceStore && id != null) voiceStore.discard(id);
	},
	// The 5-minute cap is the SERVER's limit, enforced at capture time so the audio is kept and
	// transcribed rather than refused after the fact.
	onAutoStop: () => {
		micState.value = "idle";
		notify("Recording stopped at the 5-minute limit — transcribing what you said.", {
			type: "info",
		});
	},
	// One heads-up before the cap, so the cut is something the user steers into instead of a
	// mid-sentence surprise explained only afterwards.
	onNearCap: (secondsLeft) => {
		notify(
			`${secondsLeft} seconds left before the 5-minute limit — finish your sentence and stop when you're ready.`,
			{ type: "info" }
		);
	},
	// The recorder died mid-take (device removed, tab starved, MediaRecorder error). It has
	// already salvaged the fragments through onDone; without this the mic UI would keep claiming
	// to record — frozen clock, live dot, "Stop dictation" tooltip — with no microphone behind
	// it, and the user would only find out on their next click. A start() failure is handled by
	// startMic's own notify, and micState is still 'idle' there, so this stays out of its way.
	onError: (msg) => {
		if (micState.value !== "recording") return;
		micState.value = "idle";
		notify(msg || "Recording failed. Try again.", { type: "error" });
	},
});

async function startMic() {
	// RE-ENTRY GUARD — the composer's lockout bug lived here. stopMic() flips micState to 'idle'
	// SYNCHRONOUSLY and then awaits the browser's async onstop, so during that window the button
	// reads "Dictate" and a second click (a double-click, or the very natural click straight
	// after the 5-minute auto-stop toast, or the persist-fail stop) lands right back in here
	// while the previous take is still finishing. All four conditions matter: micState covers the
	// UI, `starting` covers the permission-prompt window, the recorder's own state covers the
	// stop→onstop window, and _activeRecordingId covers the take whose unit is still open.
	if (
		micState.value === "recording" ||
		micRec.starting ||
		micRec.state === "recording" ||
		_activeRecordingId != null
	)
		return;
	// Tag the take with the on-screen scope — a real conversation id, or the stable new-chat
	// draft scope when nothing is saved yet (so recovery can't misroute it).
	_micConvId = _currentScope();
	_ensureVoiceSession();
	// start() answers whether THIS call put a recorder live. Never infer it from micRec.state:
	// during the stop→onstop window that still reads "recording" from the PREVIOUS take, which
	// is what let a second unit be minted and the first one stranded in `recording` forever —
	// voiceBusyCount then never returns to 0 and every send, voice or not, is refused.
	const started = await micRec.start();
	if (micRec.state === "error") {
		notify(micRec.error || "Couldn't start the microphone.", { type: "error" });
		return;
	}
	if (!started || micRec.state !== "recording") return;
	// Open the recording's unit only once the mic is actually live: a denied prompt must not
	// leave an empty take arming the leave guard. The first fragment is ~15 s away, so the id is
	// always in place before anything needs it.
	_activeRecordingId = voiceStore.begin({ conversationId: _micConvId });
	micState.value = "recording";
}
async function stopMic() {
	if (micState.value !== "recording") return;
	micState.value = "idle";
	// Finalise: the recorder flushes its tail, assembles the take, and hands it to onDone for
	// ONE transcription. The composer stays usable while that runs.
	await micRec.stop();
	if (micRec.state === "error")
		notify(micRec.error || "Recording failed. Try again.", { type: "error" });
}
async function cancelMic() {
	await micRec.cancel();
	micState.value = "idle";
}

// ---- failed-recording chip actions ----
// The chip means three different things and must not read the same for any two of them. Before a
// send it is a recording the user can still fix and include; after a send that went out without
// it, it is audio a message has already left behind, and Retry can only ever add to the CURRENT
// draft (the sent message is immutable); and `noSpeech` is the take nothing was heard in —
// measured silent here, or transcribed to nothing by the server — where "didn't transcribe"
// would be a riddle and the honest verb is "transcribe it anyway".
const failedChipLabel = (f) => {
	const len = _fmtClock(f.durationS);
	if (f.noSpeech) return `Recording ${len} — nothing was heard`;
	// WHY it failed, from the server's own (secret-scrubbed) message: a permanent fault
	// ("Speech-to-text is not enabled on this site.") and a transient blip are otherwise
	// indistinguishable, so users Retry-loop the same upload forever and report nothing useful.
	const why = f.error ? ` · ${f.error}` : "";
	return f.sentWithout
		? `Recording ${len} didn't transcribe — your last message went without it${why}`
		: `Recording ${len} didn't transcribe${why}`;
};
const failedChipTitle = (f) => (f.error ? `Reason: ${f.error}` : "");
const failedChipRetryLabel = (f) => (f.noSpeech ? "Transcribe anyway" : "Retry");
const failedChipRetryTitle = (f) =>
	f.sentWithout
		? "Transcribe again — the words go into your current draft, not the message that already went"
		: f.noSpeech
		? "Send it for transcription anyway — nothing audible was measured, but the measurement can be wrong"
		: "Transcribe this recording again";
function retryRecording(id) {
	if (voiceStore) voiceStore.retry(id);
}
// Stop waiting on a transcription. The recording moves to the failed chip with its audio intact
// (never a discard), which is also what releases the send block — a slow or misconfigured STT
// used to hold every send, typed ones included, for the whole client budget with no way out but
// a tab reload.
function cancelTranscribing(id) {
	if (voiceStore) voiceStore.cancelTranscription(id);
}
// Retry on an "audio not saved" chip — re-attempt the durable write. The chip clears once the
// store accepts it (e.g. after the user frees space / leaves private mode).
function retryPersistRecording(id) {
	if (voiceStore) voiceStore.retryPersist(id);
}
// ✕ on an "audio not saved" chip. This audio exists ONLY in this tab's memory — there is no
// durable copy to recover it from — so the confirm names that plainly instead of the usual
// "download it first if you want to keep it" hint, which would understate it.
async function discardUnpersistedRecording(id) {
	if (!voiceStore) return;
	const ok = await confirm({
		title: "Discard this recording?",
		message:
			"This audio could not be saved to disk — discarding loses it. It exists only in this tab, so a reload would lose it too. Download it first if you want to keep it.",
		danger: true,
		confirmLabel: "Discard",
		cancelLabel: "Keep",
	});
	if (!ok) return;
	voiceStore.discard(id);
}
function _recordingBlob(rec) {
	if (!rec) return null;
	if (rec.blob) return rec.blob;
	// Still mid-take: join what has been captured so far. Timeslice fragments concatenate into a
	// valid (truncated) webm, so a download during recording is playable, not junk.
	const parts = (rec.fragments || []).map((f) => f.blob).filter(Boolean);
	return parts.length ? new Blob(parts, { type: rec.mimeType || "audio/webm" }) : null;
}
function downloadRecording(id) {
	const rec = voiceStore && voiceStore.get(id);
	const blob = _recordingBlob(rec);
	if (blob)
		_downloadBlob(blob, `dictation-${_fmtClock(rec.durationS || 0).replace(":", "m")}s.webm`);
	else notify("There is no audio to download for that recording.", { type: "error" });
}
// Drop a permanently-failed recording — confirmed, so recoverable audio is never one-click-deleted,
// and it clears the unload-guard nag after a Download.
async function discardRecording(id) {
	if (!voiceStore) return;
	const ok = await confirm({
		title: "Discard this recording?",
		message:
			"This recording couldn't be transcribed. Discard it? Download it first if you want to keep the audio.",
		danger: true,
		confirmLabel: "Discard",
		cancelLabel: "Keep",
	});
	if (!ok) return;
	voiceStore.discard(id);
}

// ---- retained-recording chip actions ----
// A transcribed recording whose words the user edited OUT before sending. Its audio is retained
// (never lost) but would otherwise be actionless — these resolve it so the leave guard is never
// armed forever. Restore drops its transcript back into the composer for the scope it was spoken
// in, so the next send's payload match releases it.
function restoreRecording(id) {
	if (!voiceStore) return;
	const r = (voiceQ.value.retained || []).find((x) => x.id === id);
	const rec = voiceStore.get(id);
	if (!r || !rec) return;
	const t = (r.text || "").trim();
	if (t) _mutateComposerFor(_recScope(rec), (prev) => _joinAppend(prev, t));
	voiceStore.unorphan(id);
}
// Discard a retained recording: it DID transcribe (different copy from the failed-recording
// discard), so the user is choosing to drop the audio AND the words they removed.
async function discardRetainedRecording(id) {
	if (!voiceStore) return;
	const ok = await confirm({
		title: "Discard this recording?",
		message:
			"Its transcript was edited out of your message. Discard the audio too? Download it first if you want to keep it.",
		danger: true,
		confirmLabel: "Discard",
		cancelLabel: "Keep",
	});
	if (!ok) return;
	voiceStore.discard(id);
}
function _downloadBlob(blob, filename) {
	try {
		const url = URL.createObjectURL(blob);
		const a = document.createElement("a");
		a.href = url;
		a.download = filename;
		document.body.appendChild(a);
		a.click();
		a.remove();
		setTimeout(() => URL.revokeObjectURL(url), 4000);
	} catch (e) {
		notify("Couldn't download the recording.", { type: "error" });
	}
}

// ---- reload recovery (recordings left by a crashed/closed prior session) ----
// A recording's fragments are continuations of one stream, so a PREFIX of them is still a
// decodable webm: whatever survived a crash is rebuilt and offered. The ONE unrecoverable case is
// a missing FIRST fragment (it carries the container's initialisation segment) — that take can
// only be downloaded as raw bytes or discarded, never silently dropped.
function _assembleTake(t) {
	const parts = (t.fragments || []).map((f) => f.blob).filter(Boolean);
	return parts.length ? new Blob(parts, { type: t.mimeType || "audio/webm" }) : null;
}
// Recordings the user chose to deal with LATER: hidden for this tab session only, audio
// untouched. Reset on reload, exactly like the per-clip release's laterOrphans.
const _laterTakes = new Set();
// A recording whose newest fragment is still warm is held back as another tab's LIVE take
// (voiceAudioMirror's LIVE_FRAGMENT_GRACE_MS). That is also true of a tab that crashed seconds
// ago and was reloaded straight away, so re-read ONCE after the window — otherwise genuinely
// crashed audio would be silently absent from the banner until the next visit.
let _recoveryRecheck = null;
async function _loadRecovery(isRecheck) {
	try {
		const takes = await listOrphanRecordings(_voiceSessionId, session.user);
		recoveryTakes.value = (takes || []).filter(
			(t) => t.fragments && t.fragments.length && !_laterTakes.has(t.recordingId)
		);
	} catch (e) {
		recoveryTakes.value = [];
	}
	if (!isRecheck && !recoveryTakes.value.length && !_recoveryRecheck) {
		_recoveryRecheck = setTimeout(() => {
			_recoveryRecheck = null;
			void _loadRecovery(true);
		}, LIVE_FRAGMENT_GRACE_MS + 1000);
	}
}
const recoveryLabel = (t) => {
	const len = _fmtClock(t.durationS || 0);
	return t.complete
		? `A recording from your last session wasn't transcribed (${len})`
		: `A recording from your last session can't be rebuilt (${len}) — its first fragment is missing`;
};
function recoverTake(t) {
	// Never recover while recording: the composer is already filling from a live take, and the
	// banner is hidden then anyway — this is the belt-and-suspenders gate.
	if (micState.value === "recording") {
		notify("Finish the current dictation first.", { type: "info" });
		return;
	}
	if (!t || !t.complete) return;
	const blob = _assembleTake(t);
	if (!blob) return;
	_ensureVoiceSession();
	// The transcript routes to the scope it was spoken in; warn once if that isn't the composer
	// on screen. A missing conversationId maps to the new-chat scope, never "current".
	const scope = t.conversationId != null ? t.conversationId : _NEW_CHAT_SCOPE;
	voiceStore.recover([
		{
			blob,
			durationS: t.durationS,
			mimeType: t.mimeType,
			// Canonicalise a missing scope to the ONE new-chat representation BEFORE admission
			// (never a bare null): the recovered take routes to, is stored under, and is released
			// by the SAME sentinel the live new-chat composer uses.
			conversationId: scope,
			// Adopt the prior-session fragments ATOMICALLY: the mirror deletes these keys in the
			// SAME transaction as the new put, so a failed transfer can never delete the only
			// durable copy of the audio.
			_adoptKeys: t.keys,
		},
	]);
	recoveryTakes.value = recoveryTakes.value.filter((x) => x.recordingId !== t.recordingId);
	if (scope !== _currentScope()) {
		notify("Recovered dictation was added to the draft of the chat it was spoken in.", {
			type: "info",
		});
	}
}
// "Later": stop offering this one for now, keep every byte. The audio stays in the mirror and the
// banner comes back on the next reload.
function laterTake(t) {
	_laterTakes.add(t.recordingId);
	recoveryTakes.value = recoveryTakes.value.filter((x) => x.recordingId !== t.recordingId);
}
function downloadTake(t) {
	const blob = _assembleTake(t);
	if (blob)
		_downloadBlob(
			blob,
			`dictation-recovered-${_fmtClock(t.durationS || 0).replace(":", "m")}s.webm`
		);
	else notify("There is no audio to download for that recording.", { type: "error" });
}
// Destructive: permanently delete the saved audio — confirmed. The whole feature exists to
// prevent exactly a silent one-click loss.
async function discardTake(t) {
	if (micState.value === "recording") {
		notify("Finish the current dictation first.", { type: "info" });
		return;
	}
	const ok = await confirm({
		title: "Discard this recording?",
		message:
			"This permanently deletes the saved audio and can't be undone. Download it first if you want to keep it.",
		danger: true,
		confirmLabel: "Discard",
		cancelLabel: "Keep",
	});
	if (!ok) return;
	deleteOrphanRecording(t.keys);
	recoveryTakes.value = recoveryTakes.value.filter((x) => x.recordingId !== t.recordingId);
}

// ---- never-lost navigation guard ----
// Fire ONLY for genuinely-volatile work: a live take, a take starting (the permission prompt is
// open, so micState hasn't flipped yet), or a recording the store calls "live" — still
// transcribing, un-persistable, or transcribed into this volatile un-sent draft. The guard arms
// on the store's REASON, not on "anything outstanding": a terminally-failed or already-sent-
// without recording is durably mirrored and re-offered by the recovery banner, so the same
// loss-framed copy would be false for it — and a warning that cries wolf is one users learn to
// click through, weakening it for the live case that matters. "unresolved" therefore does not
// block navigation at all: its chip and the recovery banner already carry it.
function _voiceGuardReason() {
	if (micState.value === "recording" || micRec.starting) return "live";
	return (voiceStore && voiceStore.hasUnfinishedReason()) || null;
}
function _beforeUnloadVoice(e) {
	if (_voiceGuardReason() === "live") {
		e.preventDefault();
		e.returnValue = "";
		return "";
	}
}
onBeforeRouteLeave(async () => {
	if (_voiceGuardReason() !== "live") return true;
	return await confirm({
		title: "Leave with un-transcribed audio?",
		message:
			"A recording here is still being captured or transcribed, or its words are only in this unsent draft. Leaving now can lose them. Leave anyway?",
		danger: true,
		confirmLabel: "Leave",
		cancelLabel: "Stay",
	});
});

// ---- wiki nudge ("anything worth remembering?") ----
// Set by the realtime `wiki:nudge` event for the on-screen conversation; the
// card renders above the composer only while that conversation stays current.
// ref(object) is deeply reactive, so per-field writes (mode/text/saving) stick.
const nudge = ref(null); // { conversationId, entities: [{doctype,name,label,has_page}], mode: 'idle'|'recording'|'transcribing'|'edit', text, saving }
const nudgeTaEl = ref(null);
const nudgeLabels = computed(() =>
	((nudge.value && nudge.value.entities) || [])
		.map((e) => e.label || e.name)
		.filter(Boolean)
		.join(", ")
);
const nudgeRec = useAudioRecorder({
	onAutoStop: (r) => {
		notify("Recording stopped at the 5-minute limit — transcribing.", { type: "info" });
		_nudgeTranscribe(r);
	},
});
const nudgeClock = computed(() => _fmtClock(nudgeRec.durationS || 0));
async function startNudgeMic() {
	if (!nudge.value) return;
	await nudgeRec.start();
	if (nudgeRec.state === "error") {
		notify(nudgeRec.error || "Couldn't start the microphone.", { type: "error" });
		return;
	}
	if (nudgeRec.state === "recording" && nudge.value) nudge.value.mode = "recording";
}
async function stopNudgeMic() {
	if (!nudge.value || nudge.value.mode !== "recording") return;
	nudge.value.mode = "transcribing";
	const r = await nudgeRec.stop();
	if (!r || !r.blob || !r.blob.size) {
		if (nudge.value) nudge.value.mode = "idle";
		if (nudgeRec.state === "error")
			notify(nudgeRec.error || "Recording failed. Try again.", { type: "error" });
		return;
	}
	await _nudgeTranscribe(r);
}
async function _nudgeTranscribe(r) {
	if (!nudge.value) return;
	nudge.value.mode = "transcribing";
	try {
		const res = await voice.transcribeAudio(r.blob, { durationS: r.durationS });
		const text = ((res && res.text) || "").trim();
		if (!text) {
			notify("Nothing was transcribed — try again closer to the microphone.", {
				type: "info",
			});
			if (nudge.value) nudge.value.mode = "idle";
			return;
		}
		if (nudge.value) {
			nudge.value.text = text;
			nudge.value.mode = "edit";
			nextTick(() => nudgeTaEl.value?.focus());
		}
	} catch (e) {
		notifyActionError("Couldn't transcribe audio", e);
		if (nudge.value) nudge.value.mode = "idle";
	}
}
function cancelNudgeMic() {
	nudgeRec.cancel();
	if (nudge.value) nudge.value.mode = "idle";
}
function typeNudge() {
	if (!nudge.value) return;
	nudge.value.mode = "edit";
	nextTick(() => nudgeTaEl.value?.focus());
}
async function saveNudgeNote() {
	const n = nudge.value;
	if (!n || n.saving) return;
	const transcript = (n.text || "").trim();
	if (!transcript) return;
	n.saving = true;
	try {
		await voice.saveVoiceNote({
			transcript,
			context_type: "Conversation",
			conversation: n.conversationId,
			entities: JSON.stringify(n.entities || []),
			source: "Chat Nudge",
		});
		notify(`Noted — ${agentName} will remember this`, { type: "success" });
		nudge.value = null;
	} catch (e) {
		n.saving = false;
		notifyActionError("Couldn't save your note", e);
	}
}
function dismissNudge() {
	const n = nudge.value;
	if (n && (n.mode === "recording" || n.mode === "transcribing")) nudgeRec.cancel();
	nudge.value = null;
	// Best-effort: the 7-day server-side snooze shouldn't block hiding the card.
	if (n) voice.dismissWikiNudge(n.conversationId).catch(() => {});
	// the dismissal mutes a week of nudges here — say so, once, or users
	// won't know they opted out
	notify("Okay — won't ask again in this chat for a week.");
}

// ---- attachments ----
// Chat uploads EAGERLY, the moment a file is attached: send() posts the
// uploaded `{file_url, file_name}` records straight through to the backend, so
// `pendingFiles` must keep exactly that wire shape. <Composer> renders from
// display-objects instead, so adapt here rather than reshaping the payload.
// The in-flight "Uploading…" pill is just one more entry.
const composerAttachments = computed(() => {
	const chips = pendingFiles.value.map((f, i) => ({
		key: i,
		file_name: f.file_name,
		// Only images get a preview; everything else renders as a 📎 chip.
		preview_url: isImageFile(f) ? f.file_url : "",
		removable: true,
	}));
	if (uploading.value) chips.push({ key: "uploading", uploading: true });
	return chips;
});
// Shared upload path for the file picker, clipboard paste, and drag-and-drop.
// The picker and drag-drop now live inside <Composer>, which hands us the raw
// File[] via @files-added and never uploads anything itself.
async function uploadFiles(list) {
	const files = Array.from(list || []);
	if (!files.length) return;
	uploading.value = true;
	for (const f of files) {
		try {
			pendingFiles.value = [...pendingFiles.value, await api.uploadFile(f)];
		} catch (err) {
			/* skip a file that failed to upload */
		}
	}
	uploading.value = false;
	composerRef.value?.focusInput();
}
// Transient hint shown when the clipboard holds only a file PATH, not the image
// bytes (e.g. copying an image FILE from a file manager - the OS exposes only
// the path and browsers can't read the bytes from it).
const pasteHint = ref("");
let _pasteHintTimer = null;
function flashPasteHint() {
	pasteHint.value =
		"That copied the file's path, not the image. Use the 📎 button, or copy the image itself (e.g. a screenshot) and paste again.";
	if (_pasteHintTimer) clearTimeout(_pasteHintTimer);
	_pasteHintTimer = setTimeout(() => {
		pasteHint.value = "";
	}, 6000);
}
// Paste an image straight from the clipboard (screenshot / copied image) →
// upload it as an attachment, the same path as the file picker. Plain-text
// pastes are left untouched (we only preventDefault when an image is present).
async function onPaste(e) {
	const cd = e.clipboardData;
	if (!cd) return;
	const imgs = [];
	// Some browsers populate .files for a copied file; screenshots / "Copy image"
	// land in .items. Check both, .files first.
	for (const f of cd.files || []) {
		if ((f.type || "").startsWith("image/")) imgs.push(f);
	}
	if (!imgs.length) {
		for (const it of cd.items || []) {
			if (it.kind === "file" && (it.type || "").startsWith("image/")) {
				const f = it.getAsFile();
				if (f) imgs.push(f);
			}
		}
	}
	if (!imgs.length) {
		// No image bytes. If the clipboard is just a local image-file PATH/URI
		// (file-manager copy), don't dump the raw path into the box - hint the
		// user to the right method instead. Otherwise let normal text paste run.
		const text =
			(cd.getData && (cd.getData("text/uri-list") || cd.getData("text/plain"))) || "";
		if (/^\s*(file:\/\/|\/|[a-z]:\\).*\.(png|jpe?g|gif|webp|bmp|svg)\s*$/i.test(text)) {
			e.preventDefault();
			flashPasteHint();
		}
		return;
	}
	e.preventDefault();
	uploading.value = true;
	for (let i = 0; i < imgs.length; i++) {
		const f = imgs[i];
		const ext = ((f.type || "image/png").split("/")[1] || "png").split("+")[0];
		// Clipboard images come in unnamed (or all "image.png"); give each a
		// unique, descriptive name so the upload + dedup behave.
		const named = new File([f], `pasted-${Date.now()}-${i}.${ext}`, {
			type: f.type || "image/png",
		});
		try {
			pendingFiles.value = [...pendingFiles.value, await api.uploadFile(named)];
		} catch (err) {
			/* skip a file that failed to upload */
		}
	}
	uploading.value = false;
	composerRef.value?.focusInput();
}
function removeFile(i) {
	pendingFiles.value = pendingFiles.value.filter((_, idx) => idx !== i);
}
function isImageFile(f) {
	return /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test((f && (f.file_name || f.file_url)) || "");
}

// ---- mentions (@ user, / doctype·tool) ----
let _mentionSeq = 0;
function onInput() {
	histIdx.value = null; // typing exits prompt-history navigation
	const el = composerRef.value?.el;
	if (!el) return;
	const caret = el.selectionStart;
	const m = input.value.slice(0, caret).match(/(?:^|\s)([@/])([\w-]*)$/);
	if (!m) {
		mention.value = { ...mention.value, open: false };
		return;
	}
	const type = m[1];
	const query = m[2];
	mention.value = {
		open: true,
		type,
		query,
		start: caret - query.length - 1,
		items: mention.value.items,
		index: 0,
	};
	queryMentions(type, query);
}
async function queryMentions(type, query) {
	const seq = ++_mentionSeq;
	let items = [];
	try {
		if (type === "@") {
			const r = await api.searchLink("User", query);
			items = (r || []).map((x) => ({ value: x.value, sub: x.description || "user" }));
		} else {
			const q = query.toLowerCase();
			// Customer's own skills first (the "/" command, Claude-style).
			const skills = customSkills.value
				.filter((s) => s.enabled && (s.skill_name || "").includes(q))
				.map((s) => ({ value: s.skill_name, sub: "skill" }));
			const tools = jarvisTools.value
				.filter((t) => t.includes(q))
				.map((t) => ({ value: t, sub: "tool" }));
			const r = await api.searchLink("DocType", query);
			const dts = (r || []).map((x) => ({ value: x.value, sub: "doctype" }));
			items = [...skills, ...tools, ...dts].slice(0, 8);
		}
	} catch (e) {
		items = [];
	}
	if (seq !== _mentionSeq) return;
	mention.value = { ...mention.value, items, index: 0 };
}
function applyMention(item) {
	if (!item) return;
	const el = composerRef.value?.el;
	const caret = el ? el.selectionStart : input.value.length;
	const before = input.value.slice(0, mention.value.start);
	const token = mention.value.type + item.value + " ";
	input.value = before + token + input.value.slice(caret);
	mention.value = { ...mention.value, open: false };
	nextTick(() => {
		if (el) {
			const pos = (before + token).length;
			el.focus();
			el.setSelectionRange(pos, pos);
		}
	});
}

// Resync from durable state after any gap in the socket stream. Socketio
// has no replay: events published while disconnected or hidden are gone,
// so on every (re)connect and on tab-wake we refetch instead of trusting
// the stream. loadConversation already reconciles messages, restores the
// spinner from streaming flags (freshness-guarded), and drops stale
// responses.
let _lastResync = 0;
function onResync() {
	if (booting.value || !currentId.value) return;
	const now = Date.now();
	if (now - _lastResync < 2000) return; // connect + visibility often co-fire
	_lastResync = now;
	store.loadConversations();
	// Swallow a load failure for a conversation deleted mid-session: the
	// store.conversations watcher above handles resetting a vanished persisted
	// chat; here we just avoid an unhandled rejection on the dead id.
	loadConversation(currentId.value).catch(() => {});
}
function onVisibility() {
	if (document.visibilityState === "visible") onResync();
}

// ---- shell contract (§3.7): New Chat requests, external navigation and
// external deletes now arrive from outside the component. ----
// D10 — the shell sets pendingNewChat; consume + clear it here. During boot
// the flag is only marked (onMounted starts on the empty state instead of
// restoring the last conversation).
let _consumedNewChat = false;
watch(
	() => store.pendingNewChat,
	(v) => {
		if (!v) return;
		store.pendingNewChat = false;
		_consumedNewChat = true;
		if (!booting.value) newChat();
	},
	{ immediate: true }
);
// Sidebar rows navigate via /c/:id — selection now happens outside the
// component, so follow the route param.
watch(
	() => route.params.id,
	(id) => {
		if (id && id !== currentId.value) selectConversation(id);
	}
);
// External archive (deleted from the sidebar): fall back to the empty
// new-chat state when the open conversation disappears from the list.
watch(
	() => store.conversations,
	(list) => {
		if (booting.value || !currentId.value) return;
		// A fresh/unsent chat is intentionally hidden from the list — "hidden" is
		// not "deleted", so don't reset it out from under the user. This covers an
		// empty draft (no messages) AND the first send in flight (only optimistic
		// `tmp-` rows exist, no persisted row in the list yet). Only fall back when
		// a conversation that HAS persisted content vanished (archived/reaped).
		if (sending.value || messages.value.every((m) => String(m.name).startsWith("tmp-")))
			return;
		if (!list.some((c) => c.name === currentId.value)) {
			currentId.value = null;
			messages.value = [];
			// loadConversation never runs on this path — the next send adopts the
			// server id directly and the route watcher no-ops because currentId
			// already equals it — so a builder origin left here would follow the
			// user onto the next, ordinary chat.
			originPage.value = "";
			originOf.value = "";
			resetRunState();
			if (route.params.id) router.replace({ name: "Chat" });
		}
	}
);

onMounted(async () => {
	// Latency plan, Phase 1.3: bootstrap calls run concurrently and are
	// awaited in order below. (The onboarding gate moved to AppShell, D11;
	// legacy #hash deep-links moved to the router, §9.)
	const uiP = api.getChatUiSettings().catch(() => null);
	const convsP = store.loadConversations().catch(() => {});
	// Billing banner rides the memoized readiness verdict already fetched for
	// this page load - no extra round-trip. Never blocks the mount.
	billingNoticeOf()
		.then((n) => {
			billingNotice.value = n || {};
		})
		.catch(() => {});
	socket?.on("jarvis:event", onEvent);
	socket?.on("connect", onResync);
	document.addEventListener("visibilitychange", onVisibility);
	// Live tool list for the "Tools available" count + /tool autocomplete
	// (best-effort; falls back to the seeded core set on failure).
	api.listTools()
		.then((t) => {
			if (Array.isArray(t) && t.length) jarvisTools.value = t;
		})
		.catch(() => {});
	// Billing banner off the boot readiness promise (memoized, already awaited by
	// AppShell). Not awaited here: it must never delay painting the chat.
	checkReady()
		.then((r) => {
			// suspensionNotice (steps.js) now ALSO surfaces container_provisioning's
			// detail (the same drop-the-real-reason bug applied there), but this
			// banner's "Chat is paused" title + Renew CTA is billing-specific - a
			// provisioning stall or an out-of-quota LLM account has nothing to renew
			// via US. Keep it scoped to an actual lapsed subscription; the other
			// reason gets its own honest, CTA-less banner below.
			suspendedNotice.value =
				r && r.reason === "subscription_suspended" ? suspensionNotice(r) : null;
		})
		.catch(() => {});
	// Same boot promise, the container_provisioning half: readinessDetailOf reads the
	// admin's own explanation straight off account.py's is_ready_for_chat. Never
	// awaited, never blocks the mount - a slow or unreachable admin just leaves this
	// blank, same fail-open posture as the rest of readiness.js.
	readinessDetailOf()
		.then((detail) => {
			notReadyNotice.value = detail;
		})
		.catch(() => {});
	// ...and the llm_credentials half of the same boot promise. Same fail-open
	// posture: an unreachable backend leaves this false and chat behaves as before,
	// because wrongly disabling a working composer is worse than a failed send.
	needsLlmConnection()
		.then((v) => {
			noAiConnected.value = v;
		})
		.catch(() => {});
	document.addEventListener("pointerdown", onDocClick);
	window.addEventListener("keydown", onGlobalKey);
	// Never-lost guard: warn on tab close/reload while any dictated clip is
	// un-transcribed (armed/disarmed dynamically by _voiceHasUnfinished()).
	window.addEventListener("beforeunload", _beforeUnloadVoice);
	_thinkTimer = setInterval(() => {
		thinkTick.value = busy.value ? thinkTick.value + 1 : 0;
		if (busy.value) nowMs.value = Date.now();
	}, 1000);
	ui.value = (await uiP) || {};
	// Offer recovery of any recording a prior session left un-transcribed (a tab
	// crash / accidental reload) — only when dictation is actually enabled.
	// _ensureVoiceSession FIRST: it mints _voiceSessionId, which is what excludes
	// THIS tab's own fragments from the offer. Deferring it to the first startMic
	// left the id null at load, so the exclusion never applied and the banner
	// could offer — then adopt or discard — this tab's own live take.
	if (ui.value && ui.value.stt_enabled && micRec.supported) {
		_ensureVoiceSession();
		_loadRecovery();
	}
	// Load custom skills so the "/" composer menu can offer them.
	loadCustomSkills();
	// "Discuss in chat" hand-off (Review tab → chatPrefill stash). Take the
	// stash on EVERY mount — a stale prompt must never survive to pop into the
	// composer on a later, unrelated visit — but only act on it when landing
	// on the chat home (no /c/:id param).
	const prefill = takeChatPrefill();
	const applyPrefill = !route.params.id && !!(prefill && prefill.text);
	try {
		await convsP;
		if (applyPrefill) {
			// The prompt must land as the FIRST message of a FRESH conversation.
			// Restoring the last conversation here would both wipe the composer
			// (the async restore races the prefill) and risk sending a drafted
			// governance prompt into an unrelated thread's context.
			_consumedNewChat = false; // newChat() below satisfies a pending request too
			await newChat();
		} else if (route.query.new) {
			// Desk FAB deep-link (jarvis_widget.bundle.js -> config.mjs NEW_CHAT_URL):
			// ?new=1 means start a fresh conversation instead of restoring the last
			// one. Strip the query so a refresh doesn't force ANOTHER new chat.
			router.replace({ name: "Chat" });
			_consumedNewChat = false; // newChat() below satisfies a pending request too
			await newChat();
		} else if (_consumedNewChat) {
			// New Chat was requested from another route while we booted —
			// start on a fresh empty conversation instead of restoring.
			_consumedNewChat = false;
			await newChat();
		} else {
			// Restore the chat the user was last on (survives refresh + duplicated
			// tab) before falling back to the first sidebar entry, so a starred
			// chat sorting to the top never hijacks navigation away from your
			// current chat.
			let _stored = null;
			try {
				_stored = localStorage.getItem("jarvis-last-conv");
			} catch (e) {}
			const _storedValid = _stored && store.conversations.some((c) => c.name === _stored);
			const first =
				route.params.id || (_storedValid ? _stored : null) || store.conversations[0]?.name;
			if (first) {
				currentId.value = first;
				try {
					await loadConversation(first);
				} catch (e) {
					// The URL/stored conversation is gone (a reaped empty, a cleared or
					// externally-deleted chat) — don't let the unhandled rejection abort
					// the rest of boot. Drop cleanly to the welcome state and forget the
					// dead id. newChat() now mints /c/:id URLs whose empty targets are
					// hard-deleted after EMPTY_GRACE_DAYS, so a stale bookmark is routine.
					currentId.value = null;
					messages.value = [];
					originPage.value = "";
					originOf.value = "";
					try {
						localStorage.removeItem("jarvis-last-conv");
					} catch (_e) {}
					if (route.params.id) router.replace({ name: "Chat" });
				}
			}
		}
	} finally {
		booting.value = false; // reveal welcome/thread only after the first load
	}
	// The thread (and its chart skeletons) only enter the DOM now that booting is
	// false — loadConversation's earlier processMermaid pass ran against an empty
	// thread, so render the charts here once they're actually mounted.
	await nextTick();
	processMermaid();
	// After boot, never during it: probeGreeting only reads state (it never
	// creates anything), but the banner should draw only once the restored
	// conversation/welcome screen above has settled.
	probeGreeting();
	// Apply the "Discuss in chat" prefill now that booting is false and the
	// composer is mounted: drop the drafted prompt into the fresh conversation
	// started above and, per the hand-off contract, send it as the user's
	// first message (send() with no arg reads input.value — the main-composer
	// path).
	if (applyPrefill) {
		input.value = prefill.text;
		_prefillSendContext = prefill.context || null; // rides send()'s context arg
		await nextTick();
		if (prefill.autoSend) await send();
	}
	composerRef.value?.focusInput();
});
onBeforeUnmount(() => {
	socket?.off("jarvis:event", onEvent);
	socket?.off("connect", onResync);
	document.removeEventListener("visibilitychange", onVisibility);
	document.removeEventListener("pointerdown", onDocClick);
	window.removeEventListener("keydown", onGlobalKey);
	clearInterval(_thinkTimer);
	// Release the mic — navigating to another route mid-take must not leave the
	// track hot (and its duration interval ticking) behind the dead view. dispose() is
	// synchronous and also disowns a getUserMedia still awaiting the permission grant, so
	// a grant that resolves after unmount stops its tracks instead of leaking (finding 2).
	micRec?.dispose();
	// Stop the store from poking a dead composer with a late transcription; drop the
	// unload guard. The IndexedDB mirror is left intact so a reload can still recover
	// anything un-transcribed (the route guard already warned the user).
	window.removeEventListener("beforeunload", _beforeUnloadVoice);
	voiceStore?.dispose();
	if (_voiceWaitTimer) clearInterval(_voiceWaitTimer);
	if (_recoveryRecheck) clearTimeout(_recoveryRecheck);
	if (nudge.value && (nudge.value.mode === "recording" || nudge.value.mode === "transcribing"))
		nudgeRec?.cancel();
	// ChatView is the sole writer of streamingConvId (§4 contract) and its
	// socket handlers detach above — nothing can clear the flag once we're
	// gone, so navigating off mid-stream would leave the sidebar dot pulsing
	// forever. Remount reconciles from fetched state in loadConversation.
	store.streamingConvId = null;
});
// Chat-surface keyboard handling (Esc closes overlays). Ctrl+Shift+O and
// Ctrl+B are owned by the shell now (AppShell → useShortcuts, §3.1).
function onGlobalKey(e) {
	if (e.defaultPrevented) return;
	// The shell SettingsDialog is a foreground modal with its OWN Escape handler
	// on the same window target; stopPropagation can't suppress a sibling
	// listener here, so bail out entirely while it's open — otherwise this chain
	// would ALSO close an artifact panel behind it on a single Escape.
	if (store.settingsOpen) return;
	// A confirm dialog, when open, swallows Escape itself (ConfirmDialog listens in
	// capture phase and stops propagation), so this chain never sees Escape while
	// one is up — no branch for it here.
	if (e.key === "Escape" && micState.value === "recording") {
		cancelMic();
	} else if (e.key === "Escape" && nudge.value && nudge.value.mode === "recording") {
		cancelNudgeMic();
	} else if (e.key === "Escape" && artifact.value) {
		closeArtifact();
	}
}

// ---- Publish chat context to the shell settings dialog ---------------------
// The settings dialog now lives at the shell (components/shell/SettingsDialog.vue)
// and its panes read the current conversation's live stats from the store WHILE
// ChatView is mounted; on non-chat routes ChatView is gone and chatContext is
// null, so those panes degrade to —/empty exactly as before.
watchEffect(() => {
	store.setChatContext({
		conversationId: currentId.value,
		sessionStats: {
			msgCount: msgCount.value,
			userMsgCount: userMsgCount.value,
			assistantMsgCount: assistantMsgCount.value,
			sessionToolCalls: sessionToolCalls.value,
			avgTokensPerMsg: avgTokensPerMsg.value,
			convCount: convCount.value,
			starredCount: starredCount.value,
			toolCount: toolCount.value,
			recentActivity: recentActivity.value,
		},
		convAutoApply: convAutoApply.value,
		autoApplyNote: autoApplyNote.value,
		modelLabel: modelLabel.value,
		ui: ui.value,
	});
});
onMounted(() => {
	// Actions with chat side-effects the panes invoke when a chat is active.
	store.registerSettingsActions({ toggleAutoApply, clearAllHistory });
});
onUnmounted(() => {
	store.setChatContext(null);
	store.clearSettingsActions();
});
</script>

<style scoped>
/* The brand mark is now <JarvisMark> everywhere on this surface (hero, assistant
   avatars, proactive toast), so it carries its own gradient in BOTH themes.
   Deleted with this comment: a `.jv-dark .jv-logo, .jv-dark .jv-toast-ic` rule
   that force-painted the gradient with !important in dark only. It existed
   because those chips were `background: var(--cta)` — near-black in light, and
   near-WHITE in dark, which would have put a white star on a white chip. So the
   mark was near-black in light and gradient in dark: the same logo in two
   colours, and a third (flat bg-blue-500) in NotifyToaster. One mark, one
   gradient, no !important. */
/* The Send button's rules (.jv-sendbtn + the jv-send-pop keyframe) moved to
   components/chat/Composer.vue with the button itself. `.jv-iconbtn` below did
   NOT move — it is still worn by this view's header buttons and by the
   mic/attach/wiki/nudge buttons it slots back into the Composer, which carry
   THIS component's scope id. Composer keeps its own copy for its default
   attach button. */
.jv-menuitem-danger {
	color: var(--red);
}
.jv-menuitem-danger svg {
	color: var(--red);
}
.jv-menuitem-danger:hover {
	background: var(--red-bg);
}
.jv-suggest:hover {
	border-color: var(--border-2);
	background: var(--surface-1);
}
/* buttons invert to black/white on hover (theme-adaptive: black on light,
   white on dark) — var(--text)/var(--surface) flip, with an svg-stroke
   override so the icon stays visible on the inverted background.

   MUST STAY IN SYNC with the identical copy in
   `components/chat/Composer.vue` (which styles that component's own default
   attach button; slot content sent back into it carries THIS scope id, so
   Composer's copy can't reach chat's mic/attach/wiki buttons). A third,
   DELIBERATELY DIFFERENT `.jv-iconbtn` lives globally in
   `assets/settings.css` — a quiet ghost hover for the settings dialog; do not
   unify it with these. Hoisting these two into a shared sheet was evaluated
   and rejected: unscoped, their `!important` would beat settings.css's
   un-!important hover and restyle SettingsDialog's close button. Same applies
   to the `:focus-visible` and `.jv-dark` copies further down this file. */
.jv-iconbtn:hover {
	background: var(--text) !important;
	color: var(--surface) !important;
}
.jv-iconbtn:hover svg {
	stroke: var(--surface) !important;
}
.jv-ctxbtn:hover {
	background: var(--surface-2);
}
.jv-retry:hover {
	filter: brightness(0.94);
}
.jv-modelpill:hover {
	background: var(--text) !important;
	border-color: var(--text) !important;
}
.jv-modelpill:hover svg {
	stroke: var(--surface) !important;
}
.jv-modelpill:hover span {
	color: var(--surface) !important;
}
.jv-menuitem {
	display: flex;
	align-items: center;
	gap: 9px;
	width: 100%;
	padding: 7px 9px;
	border: none;
	background: transparent;
	border-radius: 7px;
	font-family: inherit;
	font-size: 12.5px;
	color: var(--text);
	cursor: pointer;
	text-align: left;
}
.jv-menuitem:hover,
.jv-menuitem.on {
	background: var(--surface-1);
}
/* jump-to-latest arrow — floats just above the composer, centered.
   It lives in .jv-composer-wrap (this view), NOT inside <Composer>, so it and
   its .jv-sd-* transition stay here. */
.jv-scrolldown {
	position: absolute;
	left: 50%;
	bottom: 100%;
	margin-bottom: 12px;
	transform: translateX(-50%);
	z-index: 20;
	width: 38px;
	height: 38px;
	display: flex;
	align-items: center;
	justify-content: center;
	padding: 0;
	border-radius: 50%;
	background: var(--surface);
	color: var(--text-2);
	border: 1px solid var(--border-2);
	box-shadow: 0 6px 20px rgba(20, 20, 30, 0.18);
	cursor: pointer;
	transition: color 0.12s, border-color 0.12s, background 0.12s, transform 0.12s,
		box-shadow 0.12s;
}
.jv-scrolldown:hover {
	color: var(--text);
	border-color: var(--text-3);
	transform: translateX(-50%) translateY(-2px);
	box-shadow: 0 9px 24px rgba(20, 20, 30, 0.22);
}
.jv-scrolldown:active {
	transform: translateX(-50%) scale(0.92);
}
.jv-sd-enter-active,
.jv-sd-leave-active {
	transition: opacity 0.18s ease, transform 0.18s ease;
}
.jv-sd-enter-from,
.jv-sd-leave-to {
	opacity: 0;
	transform: translateX(-50%) translateY(10px);
}
/* response metrics (tools · time) */
.jv-skillused {
	display: flex;
	flex-wrap: wrap;
	gap: 6px;
	margin-top: 9px;
}
.jv-skillused-chip {
	display: inline-flex;
	align-items: center;
	gap: 5px;
	padding: 2px 9px 2px 7px;
	background: var(--cta-bg);
	border: 1px solid var(--cta);
	border-radius: 20px;
	font-size: 11px;
	font-weight: 600;
	color: var(--cta);
}
.jv-meta {
	display: flex;
	align-items: center;
	gap: 14px;
	margin-top: 0;
	font-size: 11px;
	color: var(--text-3);
}
.jv-metabar {
	display: flex;
	align-items: center;
	flex-wrap: wrap;
	gap: 14px;
	margin-top: 9px;
}
.jv-metabar:empty {
	display: none;
}
/* Tool activity (openclaw-style): collapsible list of tool calls with I/O */
.jv-activity {
	margin: 0 0 10px;
	border: 1px solid var(--border);
	border-radius: 10px;
	background: var(--surface-1);
	overflow: hidden;
}
.jv-activity-head {
	display: flex;
	align-items: center;
	gap: 7px;
	width: 100%;
	padding: 7px 11px;
	background: transparent;
	border: none;
	cursor: pointer;
	font-family: inherit;
	font-size: 12px;
	color: var(--text-2);
	text-align: left;
}
.jv-activity-head:hover {
	background: var(--surface-2);
}
.jv-activity-chev {
	flex: none;
	color: var(--text-3);
	transition: transform 0.15s ease;
}
.jv-activity-chev.open {
	transform: rotate(90deg);
}
.jv-activity-count {
	font-weight: 600;
	color: var(--text);
	flex: none;
}
.jv-activity-preview {
	color: var(--text-3);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
	min-width: 0;
}
.jv-activity-body {
	border-top: 1px solid var(--border);
	padding: 5px;
	display: flex;
	flex-direction: column;
	gap: 4px;
}
.jv-tool {
	border: 1px solid var(--border);
	border-radius: 8px;
	background: var(--surface);
	overflow: hidden;
}
.jv-tool-head {
	display: flex;
	align-items: center;
	gap: 8px;
	width: 100%;
	padding: 7px 10px;
	background: transparent;
	border: none;
	cursor: pointer;
	font-family: inherit;
	font-size: 12.5px;
	color: var(--text);
	text-align: left;
}
.jv-tool-head:hover {
	background: var(--surface-1);
}
.jv-tool-dot {
	width: 7px;
	height: 7px;
	border-radius: 50%;
	flex: none;
}
.jv-tool-dot.ok {
	background: var(--green);
}
.jv-tool-dot.err {
	background: var(--red);
}
.jv-tool-dot.run {
	background: var(--amber);
	animation: jv-pulse 1s ease-in-out infinite;
}
@keyframes jv-pulse {
	0%,
	100% {
		opacity: 1;
	}
	50% {
		opacity: 0.35;
	}
}
/* dictation mic (composer + nudge card) — red while a take is live */
.jv-micbtn.rec {
	color: var(--red) !important;
}
.jv-micbtn:disabled {
	cursor: default;
}
.jv-mic-live {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	font-size: 11.5px;
	font-weight: 600;
	color: var(--red);
	font-variant-numeric: tabular-nums;
}
.jv-mic-dot {
	width: 8px;
	height: 8px;
	border-radius: 50%;
	background: var(--red);
	animation: jv-pulse 1s ease-in-out infinite;
	flex: none;
}
.jv-mic-cancel {
	display: flex;
	align-items: center;
	justify-content: center;
	width: 22px;
	height: 22px;
	border: none;
	background: transparent;
	border-radius: 6px;
	cursor: pointer;
	color: var(--text-3);
}
.jv-mic-cancel:hover {
	background: var(--surface-2);
	color: var(--text);
}
/* small text action buttons inside voice recovery banner + failed-clip chips */
.jv-voicechip-act {
	border: none;
	background: transparent;
	padding: 1px 4px;
	border-radius: 5px;
	cursor: pointer;
	font-size: 12px;
	font-weight: 600;
	color: var(--cta);
}
.jv-voicechip-act:hover {
	background: rgba(127, 127, 127, 0.16);
}
/* the ONE destructive voice action (Discard) — deliberately quieter than the safe
   Transcribe/Download/Retry beside it, so it never sits at equal visual weight. */
.jv-voicechip-quiet {
	border: none;
	background: transparent;
	padding: 1px 4px;
	border-radius: 5px;
	cursor: pointer;
	font-size: 11px;
	font-weight: 500;
	color: var(--text-3);
}
.jv-voicechip-quiet:hover {
	background: rgba(127, 127, 127, 0.16);
	color: var(--text);
}
/* wiki nudge card — own block above the composer, never inside it */
.jv-nudge {
	display: flex;
	flex-direction: column;
	gap: 8px;
	margin: 0 0 8px;
	padding: 10px 12px;
	background: var(--surface-2);
	border: 1px solid var(--border);
	border-radius: 8px;
	font-size: 13px;
	color: var(--text);
}
/* business-note greeting banner — top of the chat area, never over the composer */
.jv-greeting-banner {
	max-width: 1280px;
	margin: 0 auto;
	width: 100%;
	padding: 12px 40px 0;
}
.jv-nudge-head {
	display: flex;
	align-items: flex-start;
	gap: 8px;
}
.jv-nudge-q {
	flex: 1;
	min-width: 0;
	line-height: 1.45;
}
.jv-nudge-x {
	flex: none;
	display: flex;
	align-items: center;
	justify-content: center;
	width: 24px;
	height: 24px;
	border: none;
	background: transparent;
	border-radius: 6px;
	cursor: pointer;
	color: var(--text-3);
}
.jv-nudge-x:hover {
	background: var(--surface-1);
	color: var(--text);
}
.jv-nudge-actions {
	display: flex;
	align-items: center;
	gap: 6px;
}
.jv-nudge-type {
	border: none;
	background: transparent;
	padding: 4px 6px;
	font-family: inherit;
	font-size: 12px;
	color: var(--text-2);
	text-decoration: underline;
	cursor: pointer;
	border-radius: 6px;
}
.jv-nudge-type:hover {
	color: var(--text);
	background: var(--surface-1);
}
.jv-nudge-ta {
	width: 100%;
	border: 1px solid var(--border);
	border-radius: 7px;
	background: var(--surface);
	color: var(--text);
	font-family: inherit;
	font-size: 13px;
	line-height: 1.5;
	padding: 8px 10px;
	resize: vertical;
	min-height: 64px;
	outline: none;
}
.jv-nudge-ta:focus {
	border-color: var(--text-3);
}
.jv-nudge-foot {
	display: flex;
	justify-content: flex-end;
	gap: 6px;
}
.jv-tool-name {
	font-weight: 550;
	font-family: ui-monospace, "SF Mono", Menlo, monospace;
	font-size: 12px;
}
.jv-tool-status {
	margin-left: auto;
	font-size: 11px;
	color: var(--text-3);
}
.jv-tool-chev {
	flex: none;
	color: var(--text-3);
	transition: transform 0.15s ease;
}
.jv-tool-chev.open {
	transform: rotate(90deg);
}
.jv-tool-detail {
	padding: 4px 11px 11px;
	border-top: 1px solid var(--border);
}
.jv-tool-io-k {
	font-size: 10.5px;
	font-weight: 700;
	letter-spacing: 0.04em;
	text-transform: uppercase;
	color: var(--text-3);
	margin: 9px 0 4px;
}
.jv-tool-io {
	margin: 0;
	padding: 9px 11px;
	background: var(--surface-2);
	border: 1px solid var(--border);
	border-radius: 7px;
	font-family: ui-monospace, "SF Mono", Menlo, monospace;
	font-size: 11.5px;
	line-height: 1.5;
	color: var(--text);
	white-space: pre-wrap;
	word-break: break-word;
	overflow-x: auto;
	max-height: 320px;
	overflow-y: auto;
}
/* an orphaned tool failure surfaced inline (no assistant turn to hang it off) */
.jv-toolfail {
	display: flex;
	align-items: flex-start;
	gap: 9px;
	margin: 4px 0;
	padding: 9px 13px;
	border: 1px solid var(--red-bd, color-mix(in srgb, var(--red) 32%, var(--border)));
	border-radius: 11px;
	background: var(--red-bg, var(--surface-2));
	font-size: 13px;
	line-height: 1.45;
	color: var(--text);
}
.jv-toolfail-ico {
	flex: none;
	margin-top: 1px;
	color: var(--red);
}
.jv-toolfail-main {
	min-width: 0;
}
.jv-toolfail-title {
	font-weight: 550;
	overflow-wrap: anywhere;
}
.jv-toolfail-msg {
	margin-top: 2px;
	color: var(--text-2);
	overflow-wrap: anywhere;
}
.jv-toolfail-hint {
	margin-top: 3px;
	font-size: 12.5px;
	color: var(--text-3);
	overflow-wrap: anywhere;
}
.jv-meta span {
	display: inline-flex;
	align-items: center;
	gap: 4px;
}
/* live tool activity rows */
.jv-toolrow {
	display: flex;
	align-items: center;
	gap: 7px;
	font-size: 12.5px;
	color: var(--text-2);
	padding: 2px 0;
}
.jv-toolrow b {
	font-weight: 600;
	color: var(--text);
	font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	font-size: 12px;
}
.jv-tooldone {
	color: var(--text-3);
	font-size: 12px;
}
.jv-spin {
	animation: jv-spin 0.8s linear infinite;
}
@keyframes jv-spin {
	to {
		transform: rotate(360deg);
	}
}
/* Phase-0 admission: Cancel affordance on the queued chip. Text-button idiom
   (design.md), muted until hover. */
.jv-queued-cancel {
	appearance: none;
	background: transparent;
	border: none;
	padding: 2px 6px;
	margin: 0;
	font: inherit;
	font-size: 12px;
	color: var(--text-3);
	text-decoration: underline;
	cursor: pointer;
	border-radius: 5px;
}
.jv-queued-cancel:hover {
	color: var(--text-1);
	background: var(--surface-gray-2, rgba(0, 0, 0, 0.05));
}
/* thinking dots — classed so reduced-motion can disable them (UX #13) */
.jv-tdot {
	width: 6px;
	height: 6px;
	border-radius: 50%;
	background: var(--text-3);
	animation: jv-dot 1.1s infinite;
}
/* visually-hidden live region for screen-reader announcements (UX #5) */
.jv-sr {
	position: absolute;
	width: 1px;
	height: 1px;
	margin: -1px;
	padding: 0;
	border: 0;
	overflow: hidden;
	clip: rect(0 0 0 0);
	clip-path: inset(50%);
	white-space: nowrap;
}
/* visible keyboard focus (UX #15) */
.jv-suggest:focus-visible,
.jv-iconbtn:focus-visible,
.jv-retry:focus-visible,
.jv-modelpill:focus-visible {
	outline: 2px solid var(--cta);
	outline-offset: 2px;
}
/* honor reduced-motion on the chat surface (UX #13) */
@media (prefers-reduced-motion: reduce) {
	.jv-spin {
		animation: none;
	}
	.jv-tdot {
		animation: none;
		opacity: 0.55;
	}
	.jv-tool-dot.run,
	.jv-mic-dot {
		animation: none;
	}
	.jv-settings,
	.jv-skills-modal {
		animation: none;
	}
}
/* mobile layout (UX #12): the chat had fixed 40px desktop paddings + a 2-col
   welcome grid; inline styles win over class rules, so these override with
   !important. The "Connect phone" QR flow ships people straight here. */
@media (max-width: 640px) {
	.jv-thread-inner {
		padding: 22px 16px 28px !important;
	}
	.jv-composer-wrap {
		padding: 10px 14px 14px !important;
	}
	.jv-greeting-banner {
		padding: 10px 14px 0 !important;
	}
	.jv-welcome-grid {
		grid-template-columns: 1fr !important;
	}
	.jv-welcome-h1 {
		font-size: 24px !important;
	}
}

/* inline canvas/chart artifacts (rendered sandboxed) */
.jv-canvas {
	margin-top: 12px;
	border: 1px solid var(--border);
	border-radius: 10px;
	overflow: hidden;
	background: var(--surface);
}
.jv-chartwrap {
	margin: 10px 0;
	border: 1px solid var(--border);
	border-radius: 10px;
	padding: 8px 10px;
	background: var(--surface);
}
.jv-canvas-bar {
	display: flex;
	align-items: center;
	gap: 7px;
	padding: 8px 12px;
	font-size: 12.5px;
	font-weight: 550;
	color: var(--text-2);
	background: var(--surface-1);
	border-bottom: 1px solid var(--border);
}
.jv-canvas-bar svg {
	color: var(--text-3);
	flex: none;
}
.jv-canvas-type {
	margin-left: auto;
	font-size: 10px;
	text-transform: uppercase;
	letter-spacing: 0.04em;
	color: var(--text-3);
	border: 1px solid var(--border);
	border-radius: 4px;
	padding: 1px 5px;
}
.jv-canvas-frame {
	width: 100%;
	height: 440px;
	border: 0;
	display: block;
	background: #fff;
}
.jv-canvas-pdf {
	height: 560px;
}
.jv-canvas-img {
	display: block;
	max-width: 100%;
	height: auto;
	margin: 0 auto;
	background: #fff;
}
.jv-canvas-loading {
	padding: 26px 14px;
	text-align: center;
	font-size: 12.5px;
	color: var(--text-3);
}
.jv-canvas-dl {
	margin-left: auto;
	font-size: 11px;
	font-weight: 600;
	color: var(--cta);
	text-decoration: none;
	padding: 2px 8px;
	border: 1px solid var(--border);
	border-radius: 6px;
}
.jv-canvas-dl:hover {
	background: var(--surface-1);
}
.jv-canvas-file {
	display: flex;
	align-items: center;
	gap: 10px;
	padding: 16px 16px;
	color: var(--text-2);
	text-decoration: none;
	font-size: 13px;
}
.jv-canvas-file svg {
	color: var(--text-3);
	flex: none;
}
.jv-canvas-file:hover {
	background: var(--surface-1);
}
.jv-canvas-file b {
	font-weight: 600;
	color: var(--text);
}
.jv-cards,
.jv-action,
.jv-email {
	min-width: 0;
	max-width: 100%;
}
.jv-switch {
	width: 38px;
	height: 22px;
	flex: none;
	border: none;
	border-radius: 11px;
	background: var(--surface-3);
	position: relative;
	cursor: pointer;
	padding: 0;
	transition: background 0.15s;
}
.jv-switch.on {
	background: var(--green);
}
.jv-switch-knob {
	position: absolute;
	top: 2px;
	left: 2px;
	width: 18px;
	height: 18px;
	border-radius: 50%;
	background: #fff;
	box-shadow: 0 1px 2px rgba(0, 0, 0, 0.25);
	transition: left 0.15s;
}
.jv-switch.on .jv-switch-knob {
	left: 18px;
}
/* day separators between message groups (UX #23) */
.jv-daydivider {
	display: flex;
	align-items: center;
	justify-content: center;
	margin: 6px 0 2px;
}
.jv-daydivider span {
	font-size: 11px;
	font-weight: 550;
	color: var(--text-3);
	background: var(--surface-1);
	border: 1px solid var(--border);
	border-radius: 999px;
	padding: 2px 10px;
}
/* "You stopped this reply." - a muted rule under the body, in the same
   vocabulary as .jv-msgtime. Deliberately NOT --red: the user chose to stop,
   and dressing their own click as a failure is a lie about what happened. */
.jv-stopped {
	margin-top: 8px;
	padding-top: 7px;
	border-top: 1px solid var(--border);
	font-size: 11.5px;
	color: var(--text-3);
}

/* ===== settings panel (slide-over console) ===== */
/* The settings modal's CSS lived here (.jv-settings-overlay / .jv-settings, a
   760px shell) until the dialog was HOISTED to components/shell/SettingsDialog.vue
   — see the template note near the top of this file. These rules are <style scoped>,
   so once ChatView stopped rendering the dialog they matched nothing: edits to them
   silently did nothing while the live styles came from @/assets/settings.css.
   Removed rather than left as a decoy. Style the dialog in assets/settings.css.
   (jv-popin STAYS — .jv-skills-modal still animates with it. The confirm dialog
   moved to components/shell/ConfirmDialog.vue with its own jv-confirm-popin.) */
@keyframes jv-popin {
	from {
		transform: scale(0.97);
		opacity: 0;
	}
	to {
		transform: scale(1);
		opacity: 1;
	}
}
.jv-settings-nav {
	width: 196px;
	flex: none;
	background: var(--surface-1);
	border-right: 1px solid var(--border);
	padding: 14px 10px;
	display: flex;
	flex-direction: column;
	gap: 2px;
}
.jv-settings-nav-title {
	font-size: 15px;
	font-weight: 700;
	color: var(--text);
	padding: 4px 10px 12px;
}
.jv-settings-navitem {
	display: flex;
	align-items: center;
	gap: 9px;
	width: 100%;
	padding: 8px 10px;
	border: none;
	background: transparent;
	border-radius: 8px;
	font-family: inherit;
	font-size: 13px;
	font-weight: 500;
	color: var(--text-2);
	cursor: pointer;
	text-align: left;
}
.jv-settings-navitem svg {
	color: var(--text-3);
	flex: none;
}
.jv-settings-navitem:hover {
	background: var(--surface-2);
	color: var(--text);
}
.jv-settings-navitem.on {
	background: var(--surface-3);
	color: var(--text);
}
.jv-settings-navitem.on svg {
	color: var(--text);
}
.jv-settings-main {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-direction: column;
}
.jv-settings-head {
	display: flex;
	align-items: center;
	justify-content: space-between;
	padding: 15px 18px;
	border-bottom: 1px solid var(--border);
	flex: none;
}
.jv-settings-body {
	flex: 1;
	overflow-y: auto;
	padding: 18px 22px 28px;
}
.jv-set-sec {
	font-size: 11px;
	font-weight: 600;
	color: var(--text-3);
	text-transform: uppercase;
	letter-spacing: 0.04em;
	margin: 0 0 8px;
}
/* openclaw-style usage stat cards */
.jv-statgrid {
	display: grid;
	grid-template-columns: 1fr 1fr;
	gap: 11px;
}
.jv-stat {
	background: var(--surface-1);
	border: 1px solid var(--border);
	border-radius: 12px;
	padding: 13px 15px;
}
.jv-stat-label {
	font-size: 10px;
	font-weight: 600;
	letter-spacing: 0.05em;
	text-transform: uppercase;
	color: var(--text-3);
}
.jv-stat-val {
	font-size: 22px;
	font-weight: 650;
	color: var(--text);
	margin-top: 5px;
	line-height: 1.05;
}
.jv-stat-sub {
	font-size: 11px;
	color: var(--text-3);
	margin-top: 3px;
}
.jv-set-row {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 12px;
	padding: 9px 0;
	border-bottom: 1px solid var(--border);
	font-size: 13px;
	color: var(--text-2);
}
.jv-set-row:last-child {
	border-bottom: 0;
}
.jv-set-row b {
	font-weight: 600;
	color: var(--text);
	text-align: right;
}
.jv-set-empty {
	font-size: 12.5px;
	color: var(--text-3);
	padding: 14px 0;
}
.jv-set-hint {
	font-size: 11.5px;
	color: var(--text-3);
	margin-top: 9px;
}
.jv-kbd-row {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 12px;
	padding: 9px 0;
	border-bottom: 1px solid var(--border);
	font-size: 13px;
	color: var(--text-2);
}
.jv-kbd-row:last-of-type {
	border-bottom: 0;
}
.jv-kbd {
	display: inline-flex;
	align-items: center;
	justify-content: center;
	min-width: 22px;
	height: 22px;
	padding: 0 6px;
	background: var(--surface-1);
	border: 1px solid var(--border-2);
	border-bottom-width: 2px;
	border-radius: 6px;
	font-family: ui-monospace, "SF Mono", Menlo, monospace;
	font-size: 11.5px;
	font-weight: 600;
	color: var(--text);
}
.jv-kbd-plus {
	color: var(--text-3);
	margin: 0 3px;
	font-size: 11px;
}
.jv-est {
	font-size: 9.5px;
	font-weight: 600;
	text-transform: uppercase;
	letter-spacing: 0.04em;
	color: var(--text-3);
	border: 1px solid var(--border);
	border-radius: 4px;
	padding: 1px 5px;
}
.jv-usage-bar {
	margin-top: 12px;
	height: 7px;
	border-radius: 99px;
	background: var(--surface-2);
	overflow: hidden;
}
.jv-usage-fill {
	height: 100%;
	border-radius: 99px;
	background: var(--cta);
	transition: width 0.25s ease;
}
/* custom skills */
.jv-skill-btn {
	padding: 6px 12px;
	background: var(--cta);
	border: 1px solid var(--cta);
	border-radius: 8px;
	font-family: inherit;
	font-size: 12.5px;
	font-weight: 600;
	color: var(--cta-fg);
	cursor: pointer;
	white-space: nowrap;
	transition: opacity 0.12s;
}
.jv-skill-btn:hover {
	opacity: 0.9;
}
.jv-skill-btn:disabled {
	opacity: 0.5;
	cursor: default;
}
.jv-skill-btn.ghost {
	background: transparent;
	color: var(--text-2);
	border-color: var(--border-2);
}
.jv-skill-form {
	border: 1px solid var(--border);
	border-radius: 10px;
	padding: 14px;
	margin: 6px 0 14px;
	background: var(--surface-1);
}
.jv-skill-err {
	font-size: 12px;
	color: var(--red);
	background: var(--red-bg);
	border: 1px solid var(--red-bd);
	border-radius: 7px;
	padding: 7px 10px;
	margin-bottom: 10px;
}
.jv-skill-l {
	display: block;
	font-size: 11px;
	font-weight: 600;
	color: var(--text-3);
	text-transform: uppercase;
	letter-spacing: 0.04em;
	margin: 0 0 4px;
}
.jv-skill-in,
.jv-skill-ta {
	width: 100%;
	box-sizing: border-box;
	padding: 8px 10px;
	background: var(--surface-2);
	border: 1px solid var(--border);
	border-radius: 8px;
	font-family: inherit;
	font-size: 13px;
	color: var(--text);
	outline: none;
}
.jv-skill-ta {
	font-family: ui-monospace, "SF Mono", Menlo, monospace;
	font-size: 12px;
	resize: vertical;
	line-height: 1.5;
}
.jv-skill-in:focus,
.jv-skill-ta:focus {
	border-color: var(--cta);
}
.jv-skill-in:disabled {
	color: var(--text-3);
}
.jv-skill-row {
	display: flex;
	align-items: center;
	gap: 8px;
	margin: 0 -10px;
	padding: 11px 10px;
	border-bottom: 1px solid var(--border);
	border-radius: 10px;
	transition: background 0.12s;
}
.jv-skill-row:last-child {
	border-bottom: 0;
}
.jv-skill-row:hover {
	background: var(--surface-1);
}
/* ============ PREDEFINED PREMIUM BUTTON ============
   Reusable across the app. Compose a variant with the base:
     <button class="jv-btn jv-btn--primary">Save</button>
     <button class="jv-btn jv-btn--ghost">Cancel</button>
     <button class="jv-btn jv-btn--danger">Delete</button>
     <button class="jv-btn jv-btn--icon">…</button>   (icon-only)
   Add --sm for a compact size (header actions). */
.jv-btn {
	flex: none;
	display: inline-flex;
	align-items: center;
	justify-content: center;
	gap: 6px;
	height: 34px;
	padding: 0 15px;
	border-radius: 10px;
	border: 1px solid transparent;
	background: transparent;
	font-family: inherit;
	font-size: 12.5px;
	font-weight: 600;
	line-height: 1;
	white-space: nowrap;
	cursor: pointer;
	user-select: none;
	transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease,
		box-shadow 0.15s ease, transform 0.12s ease;
}
.jv-btn:active {
	transform: scale(0.97);
}
.jv-btn:disabled {
	opacity: 0.5;
	cursor: default;
	pointer-events: none;
	box-shadow: none;
	transform: none;
}
.jv-btn svg {
	flex: none;
}
.jv-btn--primary {
	background: var(--cta);
	border-color: var(--cta);
	color: var(--cta-fg);
	box-shadow: 0 1px 2px rgba(20, 20, 30, 0.12);
}
.jv-btn--primary:hover {
	transform: translateY(-1px);
	box-shadow: 0 6px 16px rgba(20, 20, 30, 0.18);
}
.jv-btn--primary svg {
	stroke: var(--cta-fg);
}
.jv-btn--ghost {
	background: var(--surface);
	border-color: var(--border-2);
	color: var(--text-2);
}
.jv-btn--ghost:hover {
	background: var(--surface-2);
	border-color: var(--border);
	color: var(--text);
	transform: translateY(-1px);
}
.jv-btn--danger {
	background: var(--red);
	border-color: var(--red);
	color: #fff;
	box-shadow: 0 1px 2px rgba(220, 38, 38, 0.14);
}
.jv-btn--danger:hover {
	transform: translateY(-1px);
	box-shadow: 0 6px 16px rgba(220, 38, 38, 0.24);
}
.jv-btn--danger svg {
	stroke: #fff;
}
/* Soft/outline danger for the settings "Danger zone": clearly visible on the
   white theme (tinted fill + red text + red border), filling solid red on
   hover. Dedicated class so the solid .jv-btn--danger (confirm dialog) is left
   as-is. */
.jv-btn--danger-soft {
	background: var(--red-bg);
	border-color: var(--red-bd);
	color: var(--red);
}
.jv-btn--danger-soft:hover {
	background: var(--red);
	border-color: var(--red);
	color: #fff;
}
.jv-btn--sm {
	height: 32px;
	padding: 0 12px;
	font-size: 12px;
	border-radius: 9px;
}
.jv-btn--icon {
	width: 32px;
	height: 32px;
	padding: 0;
	border-radius: 9px;
	color: var(--text-3);
}
.jv-btn--icon:hover {
	background: var(--surface-2);
	color: var(--text);
	transform: none;
}
.jv-btn--icon:active {
	transform: scale(0.92);
}
/* row action icons (skills/macros): color-coded semantic hovers + springy pop */
.jv-btn--icon.jv-ib:hover {
	transform: translateY(-1px);
}
.jv-btn--icon.jv-ib:active {
	transform: scale(0.88);
}
.jv-btn--icon.jv-ib-accent:hover {
	background: var(--cta-bg);
	color: var(--cta);
	border-color: var(--cta-bd);
}
.jv-btn--icon.jv-ib-danger:hover {
	background: var(--red-bg);
	color: var(--red);
	border-color: var(--red-bd);
}

.jv-newpill {
	flex: none;
	display: inline-flex;
	align-items: center;
	gap: 5px;
	padding: 6px 12px 6px 10px;
	background: var(--cta);
	border: 1px solid var(--cta);
	border-radius: 9px;
	font-family: inherit;
	font-size: 12.5px;
	font-weight: 600;
	color: var(--cta-fg);
	cursor: pointer;
	transition: opacity 0.12s, transform 0.06s;
}
.jv-newpill:hover {
	opacity: 0.92;
}
.jv-newpill:active {
	transform: scale(0.97);
}
.jv-newpill svg {
	stroke: var(--cta-fg);
}
.jv-skill-name {
	font-size: 13px;
	font-weight: 600;
	color: var(--text);
	font-family: ui-monospace, "SF Mono", Menlo, monospace;
}
.jv-skill-off {
	font-size: 9.5px;
	font-weight: 600;
	text-transform: uppercase;
	letter-spacing: 0.04em;
	color: var(--text-3);
	border: 1px solid var(--border);
	border-radius: 4px;
	padding: 1px 5px;
	margin-left: 6px;
	font-family: inherit;
}
.jv-skill-desc {
	font-size: 12px;
	color: var(--text-3);
	margin-top: 2px;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
/* skill sharing: chips, dividers, read-only + share modal */
.jv-shared-chip {
	display: inline-flex;
	align-items: center;
	gap: 4px;
	margin-left: 8px;
	padding: 1px 7px 1px 6px;
	background: var(--cta-bg);
	color: var(--cta);
	border-radius: 999px;
	font-size: 10px;
	font-weight: 600;
	font-family: inherit;
	letter-spacing: 0.01em;
	vertical-align: middle;
}
.jv-shared-chip svg {
	stroke: var(--cta);
}
.jv-sharedby-chip {
	display: inline-flex;
	align-items: center;
	gap: 4px;
	margin-left: 8px;
	padding: 1px 7px 1px 6px;
	background: var(--surface-2);
	color: var(--text-3);
	border: 1px solid var(--border);
	border-radius: 999px;
	font-size: 10px;
	font-weight: 600;
	font-family: inherit;
	vertical-align: middle;
}
.jv-share-divider {
	margin: 14px -10px 4px;
	padding: 6px 10px 4px;
	font-size: 10.5px;
	font-weight: 700;
	text-transform: uppercase;
	letter-spacing: 0.05em;
	color: var(--text-3);
	border-top: 1px solid var(--border);
}
.jv-skill-row-shared {
	opacity: 0.96;
}
.jv-share-lock {
	flex: none;
	display: flex;
	align-items: center;
	justify-content: center;
	width: 28px;
	height: 28px;
	border-radius: 8px;
	background: var(--surface-2);
	color: var(--text-3);
	border: 1px solid var(--border);
}
.jv-ro-banner {
	display: flex;
	align-items: center;
	gap: 8px;
	padding: 9px 11px;
	margin-bottom: 13px;
	background: var(--cta-bg);
	border: 1px solid var(--cta);
	border-radius: 9px;
	font-size: 12.5px;
	color: var(--text-2);
}
.jv-ro-banner svg {
	stroke: var(--cta);
	flex: none;
}
.jv-ro-banner b {
	color: var(--text);
	font-weight: 600;
}
/* share modal */
.jv-share-modal {
	height: auto;
	max-height: 82vh;
}
.jv-share-chips {
	display: flex;
	flex-wrap: wrap;
	gap: 7px;
	margin-bottom: 12px;
}
.jv-share-chip {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	padding: 3px 6px 3px 3px;
	background: var(--surface-1);
	border: 1px solid var(--border-2);
	border-radius: 999px;
	font-size: 12px;
	color: var(--text);
	box-shadow: 0 1px 2px rgba(20, 20, 30, 0.05);
}
.jv-share-chip-name {
	font-weight: 500;
	max-width: 160px;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-share-chip-x {
	flex: none;
	width: 18px;
	height: 18px;
	display: flex;
	align-items: center;
	justify-content: center;
	border: none;
	background: transparent;
	border-radius: 50%;
	color: var(--text-3);
	cursor: pointer;
	padding: 0;
	transition: background 0.12s, color 0.12s;
}
.jv-share-chip-x:hover {
	background: var(--red-bg);
	color: var(--red);
}
.jv-share-avatar {
	flex: none;
	width: 26px;
	height: 26px;
	border-radius: 50%;
	display: flex;
	align-items: center;
	justify-content: center;
	background: var(--cta);
	color: var(--cta-fg);
	font-size: 10.5px;
	font-weight: 600;
	letter-spacing: 0.01em;
	box-shadow: 0 1px 2px rgba(79, 70, 229, 0.3);
}
.jv-share-searchwrap {
	display: flex;
	align-items: center;
	gap: 8px;
	padding: 8px 11px;
	background: var(--surface-2);
	border: 1px solid var(--border);
	border-radius: 9px;
	margin-bottom: 10px;
}
.jv-share-searchwrap:focus-within {
	border-color: var(--cta);
}
.jv-share-search {
	flex: 1;
	border: none;
	outline: none;
	background: transparent;
	font-family: inherit;
	font-size: 13px;
	color: var(--text);
}
.jv-share-list {
	display: flex;
	flex-direction: column;
	gap: 2px;
	max-height: 280px;
	overflow-y: auto;
	margin: 0 -6px;
}
.jv-share-row {
	display: flex;
	align-items: center;
	gap: 10px;
	width: 100%;
	padding: 8px 10px;
	background: transparent;
	border: none;
	border-radius: 9px;
	cursor: pointer;
	text-align: left;
	transition: background 0.12s;
}
.jv-share-row:hover {
	background: var(--surface-1);
}
.jv-share-row.on {
	background: var(--cta-bg);
}
.jv-share-row-info {
	display: flex;
	flex-direction: column;
	min-width: 0;
	flex: 1;
	line-height: 1.25;
}
.jv-share-row-name {
	font-size: 13px;
	font-weight: 550;
	color: var(--text);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-share-row-id {
	font-size: 11px;
	color: var(--text-3);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-share-check {
	flex: none;
}
.jv-share-helper {
	display: flex;
	align-items: center;
	gap: 7px;
	margin-top: 13px;
	padding: 9px 11px;
	background: var(--surface-1);
	border: 1px solid var(--border);
	border-radius: 9px;
	font-size: 11.5px;
	color: var(--text-3);
	line-height: 1.4;
}
.jv-share-helper svg {
	stroke: var(--text-3);
	flex: none;
}
/* centered skills popup */
.jv-skills-overlay {
	position: absolute;
	inset: 0;
	z-index: 62;
	background: rgba(15, 15, 22, 0.34);
	display: flex;
	align-items: center;
	justify-content: center;
	padding: 24px;
}
.jv-dark .jv-skills-overlay {
	background: rgba(0, 0, 0, 0.55);
}
.jv-skills-modal {
	width: 760px;
	max-width: 100%;
	height: 560px;
	max-height: 88vh;
	display: flex;
	flex-direction: column;
	background: var(--surface);
	border: 1px solid var(--border);
	border-radius: 14px;
	overflow: hidden;
	box-shadow: 0 24px 70px rgba(20, 20, 30, 0.28);
	animation: jv-popin 0.16s ease;
}
.jv-skills-head {
	flex: none;
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: 12px;
	padding: 15px 16px 14px 20px;
	border-bottom: 1px solid var(--border);
}
.jv-skills-head > div:first-child {
	flex: 1 1 auto;
	min-width: 0;
	align-self: center;
}
.jv-skills-title {
	font-size: 15px;
	font-weight: 600;
	color: var(--text);
	letter-spacing: -0.01em;
}
.jv-skills-sub {
	font-size: 11.5px;
	color: var(--text-3);
	margin-top: 2px;
}
.jv-skills-status {
	flex: none;
	display: flex;
	align-items: center;
	gap: 7px;
	font-size: 11.5px;
	color: var(--text-3);
	padding: 11px 20px;
	background: var(--surface-1);
	border-bottom: 1px solid var(--border);
}
.jv-skills-status.ok {
	color: var(--green);
}
.jv-skills-status.err {
	color: var(--red);
}
.jv-skills-body {
	flex: 1;
	overflow-y: auto;
	padding: 16px 20px 20px;
}
.jv-skill-newrow {
	display: flex;
	align-items: center;
	gap: 8px;
	width: 100%;
	justify-content: center;
	padding: 10px;
	margin-bottom: 12px;
	background: var(--cta-bg);
	border: 1px dashed var(--cta);
	border-radius: 10px;
	font-family: inherit;
	font-size: 13px;
	font-weight: 600;
	color: var(--cta);
	cursor: pointer;
}
.jv-skill-newrow:hover {
	background: var(--cta);
	color: var(--cta-fg);
}
.jv-skill-newrow:hover svg {
	stroke: var(--cta-fg);
}
.jv-skill-formfoot {
	display: flex;
	align-items: center;
	gap: 10px;
	margin-top: 15px;
	flex-wrap: wrap;
}
.jv-skill-foothint {
	font-size: 11px;
	color: var(--text-3);
}
.jv-skill-dot {
	width: 7px;
	height: 7px;
	border-radius: 99px;
	background: var(--text-3);
	flex: none;
}
.jv-skill-dot.ok {
	background: var(--green);
}
.jv-skill-dot.err {
	background: var(--red);
}
.jv-skill-dot.spin {
	border: 2px solid var(--border-2);
	border-top-color: var(--cta);
	background: transparent;
	width: 11px;
	height: 11px;
	animation: jv-spin 0.7s linear infinite;
}
@keyframes jv-spin {
	to {
		transform: rotate(360deg);
	}
}
/* theme segmented control */
.jv-seg {
	display: flex;
	gap: 6px;
}
.jv-seg button {
	flex: 1;
	display: flex;
	flex-direction: column;
	align-items: center;
	gap: 5px;
	padding: 11px 6px;
	background: var(--surface-1);
	border: 1px solid var(--border);
	border-radius: 9px;
	font-family: inherit;
	font-size: 11.5px;
	font-weight: 550;
	color: var(--text-2);
	cursor: pointer;
	transition: border-color 0.12s, background 0.12s, color 0.12s;
}
.jv-seg button:hover {
	border-color: var(--border-2);
}
.jv-seg button.on {
	border-color: var(--cta);
	color: var(--text);
	background: var(--cta-bg);
}
.jv-seg button svg {
	color: var(--text-3);
}
.jv-seg button.on svg {
	color: var(--cta);
}
/* danger / delete */
.jv-danger {
	display: flex;
	align-items: center;
	gap: 8px;
	width: 100%;
	justify-content: center;
	padding: 9px 12px;
	background: var(--red-bg);
	border: 1px solid var(--red-bd);
	border-radius: 9px;
	font-family: inherit;
	font-size: 13px;
	font-weight: 550;
	color: var(--red);
	cursor: pointer;
}
.jv-danger:hover:not(:disabled) {
	filter: brightness(0.97);
}
.jv-danger:disabled {
	opacity: 0.5;
	cursor: default;
}
/* activity rows */
.jv-act {
	padding: 10px 0;
	border-bottom: 1px solid var(--border);
}
.jv-act:last-child {
	border-bottom: 0;
}
.jv-act-top {
	display: flex;
	align-items: center;
	gap: 7px;
	font-size: 12.5px;
	font-weight: 550;
	color: var(--text-2);
}
.jv-act-ms {
	margin-left: auto;
	font-variant-numeric: tabular-nums;
	color: var(--text-3);
	font-weight: 500;
}
.jv-act-names {
	font-size: 11.5px;
	color: var(--text-3);
	margin-top: 3px;
	font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
	word-break: break-word;
}
/* macro run history dashboard (settings → Macro runs) */
.jv-runfilters {
	display: flex;
	align-items: center;
	gap: 10px;
	margin: 16px 0 6px;
	flex-wrap: wrap;
}
.jv-runchips {
	display: inline-flex;
	background: var(--surface-1);
	border: 1px solid var(--border);
	border-radius: 9px;
	padding: 3px;
	gap: 2px;
}
.jv-runchips button {
	font-family: inherit;
	font-size: 12px;
	font-weight: 550;
	padding: 5px 11px;
	border-radius: 6px;
	color: var(--text-3);
	cursor: pointer;
	border: none;
	background: transparent;
}
.jv-runchips button.on {
	background: var(--surface-3);
	color: var(--text);
}
.jv-runmacrosel {
	margin-left: auto;
	font-family: inherit;
	font-size: 12px;
	color: var(--text-2);
	background: var(--surface-1);
	border: 1px solid var(--border);
	border-radius: 8px;
	padding: 6px 10px;
	cursor: pointer;
	outline: none;
}
.jv-runmacrosel:focus {
	border-color: var(--cta);
}
.jv-run {
	display: flex;
	align-items: flex-start;
	gap: 11px;
	padding: 12px 2px;
	border-bottom: 1px solid var(--surface-2);
}
.jv-run:last-of-type {
	border-bottom: none;
}
.jv-run-dot {
	flex: none;
	width: 9px;
	height: 9px;
	border-radius: 50%;
	margin-top: 5px;
}
.jv-run-dot.d-ok {
	background: var(--green);
}
.jv-run-dot.d-err {
	background: var(--red);
}
.jv-run-dot.d-run {
	background: var(--cta);
	box-shadow: 0 0 0 3px var(--cta-bg);
}
.jv-run-dot.d-stop {
	background: var(--text-3);
}
.jv-run-main {
	flex: 1;
	min-width: 0;
}
.jv-run-top {
	display: flex;
	align-items: center;
	gap: 9px;
	flex-wrap: wrap;
}
.jv-run-name {
	font-size: 13.5px;
	font-weight: 600;
	color: var(--text);
}
.jv-run-badge {
	font-size: 10.5px;
	font-weight: 600;
	padding: 2px 8px;
	border-radius: 99px;
	text-transform: capitalize;
}
.jv-run-badge.b-ok {
	background: var(--green-bg);
	color: var(--green);
	border: 1px solid var(--green-bd);
}
.jv-run-badge.b-err {
	background: var(--red-bg);
	color: var(--red);
	border: 1px solid var(--red-bd);
}
.jv-run-badge.b-run {
	background: var(--cta-bg);
	color: var(--cta);
	border: 1px solid var(--cta-bd);
}
.jv-run-badge.b-stop {
	background: var(--surface-2);
	color: var(--text-3);
	border: 1px solid var(--border-2);
}
.jv-run-trig {
	display: inline-flex;
	align-items: center;
	gap: 4px;
	font-size: 10.5px;
	color: var(--text-3);
}
.jv-run-meta {
	display: flex;
	align-items: center;
	gap: 8px;
	font-size: 11.5px;
	color: var(--text-3);
	margin-top: 4px;
	flex-wrap: wrap;
}
.jv-run-prog {
	font-family: ui-monospace, Menlo, monospace;
}
.jv-run-sep {
	opacity: 0.5;
}
.jv-run-err {
	display: flex;
	align-items: center;
	gap: 5px;
	margin-top: 5px;
	font-size: 11.5px;
	color: var(--red);
	word-break: break-word;
}
.jv-run-err svg {
	flex: none;
}
.jv-run-act {
	display: flex;
	align-items: center;
	gap: 6px;
	flex: none;
}
.jv-run-btn {
	display: inline-flex;
	align-items: center;
	gap: 5px;
	font-family: inherit;
	font-size: 11.5px;
	font-weight: 550;
	padding: 5px 11px;
	border-radius: 7px;
	cursor: pointer;
	background: var(--surface);
	color: var(--text-2);
	border: 1px solid var(--border-2);
}
.jv-run-btn:hover {
	color: var(--text);
	border-color: var(--text-3);
}
.jv-run-btn.stop {
	background: var(--red-bg);
	color: var(--red);
	border-color: var(--red-bd);
}
.jv-run-btn.stop:hover {
	background: var(--red);
	color: #fff;
	border-color: var(--red);
}
.jv-run-loadmore {
	display: block;
	margin: 14px auto 2px;
	font-family: inherit;
	font-size: 12px;
	font-weight: 550;
	color: var(--text-2);
	background: var(--surface-1);
	border: 1px solid var(--border);
	border-radius: 8px;
	padding: 8px 18px;
	cursor: pointer;
}
.jv-run-loadmore:disabled {
	opacity: 0.6;
	cursor: default;
}
/* fade for the overlay */
.jv-fade-enter-active,
.jv-fade-leave-active {
	transition: opacity 0.16s ease;
}
.jv-fade-enter-from,
.jv-fade-leave-to {
	opacity: 0;
}

/* artifact card (in the message) */
.jv-artifact {
	display: flex;
	align-items: center;
	gap: 11px;
	width: 100%;
	margin-top: 12px;
	padding: 10px 12px;
	border: 1px solid var(--border);
	border-radius: 10px;
	background: var(--surface);
	cursor: pointer;
	text-align: left;
	font-family: inherit;
	transition: border-color 0.12s, background 0.12s;
}
.jv-artifact:hover {
	border-color: var(--border-2);
	background: var(--surface-1);
}
.jv-artifact-ic {
	flex: none;
	width: 34px;
	height: 34px;
	border-radius: 8px;
	display: flex;
	align-items: center;
	justify-content: center;
	background: var(--cta-bg);
	color: var(--cta);
}
.jv-artifact-ic.t-pdf {
	background: var(--red-bg);
	color: var(--red);
}
.jv-artifact-ic.t-image {
	background: var(--green-bg);
	color: var(--green);
}
.jv-artifact-ic.t-html,
.jv-artifact-ic.t-svg {
	background: var(--amber-bg);
	color: var(--amber);
}
.jv-artifact-txt {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-direction: column;
	gap: 1px;
}
.jv-artifact-title {
	font-size: 13px;
	font-weight: 550;
	color: var(--text);
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
}
.jv-artifact-sub {
	font-size: 10px;
	color: var(--text-3);
	text-transform: uppercase;
	letter-spacing: 0.04em;
}
.jv-artifact-go {
	color: var(--text-3);
	flex: none;
}
/* the card plus its follow-on action ("Open in Dashboards"), so the button is a
   SIBLING of the card, never nested inside that <button> */
.jv-artifact-group {
	display: flex;
	flex-direction: column;
	align-items: flex-start;
}
/* secondary by design.md's rule (one solid action per surface): the card is the
   primary affordance, this is the quieter way out to the builder */
.jv-artifact-open {
	display: inline-flex;
	align-items: center;
	gap: 6px;
	margin-top: 6px;
	padding: 5px 10px;
	border: 1px solid var(--border);
	border-radius: 8px;
	background: var(--surface-1);
	font-family: inherit;
	font-size: 12px;
	font-weight: 550;
	color: var(--text-2);
	cursor: pointer;
	transition: border-color 0.12s, background 0.12s, color 0.12s;
}
.jv-artifact-open:hover {
	border-color: var(--border-2);
	background: var(--surface-2);
	color: var(--text);
}

/* confirm / cancel card for a pending ERP-mutating action */
.jv-confirm {
	display: flex;
	align-items: center;
	gap: 10px;
	margin-top: 12px;
	padding: 10px 12px;
	border: 1px solid var(--amber-bd);
	background: var(--amber-bg);
	border-radius: 10px;
}
.jv-confirm > svg {
	flex: none;
}
.jv-confirm-label {
	flex: 1;
	min-width: 0;
	font-size: 13px;
	font-weight: 550;
	color: var(--text);
}
.jv-confirm-no,
.jv-confirm-yes {
	font-family: inherit;
	font-size: 12.5px;
	font-weight: 600;
	padding: 6px 14px;
	border-radius: 7px;
	cursor: pointer;
	border: 1px solid var(--border-2);
	flex: none;
}
.jv-confirm-no {
	background: var(--surface);
	color: var(--text-2);
}
.jv-confirm-no:hover {
	background: var(--text);
	color: var(--surface);
	border-color: var(--text);
}
.jv-confirm-yes {
	background: var(--cta);
	color: var(--cta-fg);
	border-color: var(--cta);
}
.jv-confirm-yes:hover {
	background: var(--text);
	color: var(--surface);
	border-color: var(--text);
}

/* scrollable record cards (alternative to a wide table) */
.jv-cards {
	margin-top: 12px;
}
.jv-cards-title {
	font-size: 12px;
	font-weight: 600;
	color: var(--text-2);
	margin-bottom: 8px;
}
.jv-cards-strip {
	display: flex;
	gap: 10px;
	overflow-x: auto;
	padding-bottom: 6px;
	scroll-snap-type: x proximity;
	max-width: 100%;
}
.jv-cards-pager {
	display: flex;
	align-items: center;
	gap: 10px;
	margin-top: 6px;
}
.jv-cards-pgbtn {
	width: 26px;
	height: 26px;
	border: 1px solid var(--border-2);
	border-radius: 7px;
	background: var(--surface-1);
	color: var(--text-2);
	font-size: 15px;
	line-height: 1;
	cursor: pointer;
}
.jv-cards-pgbtn:disabled {
	opacity: 0.35;
	cursor: default;
}
.jv-cards-pginfo {
	font-size: 11.5px;
	color: var(--text-3);
	font-variant-numeric: tabular-nums;
}
.jv-cards-strip::-webkit-scrollbar {
	height: 7px;
}
.jv-cards-strip::-webkit-scrollbar-thumb {
	background: var(--border-2);
	border-radius: 99px;
}
.jv-card {
	flex: none;
	width: 210px;
	scroll-snap-align: start;
	box-sizing: border-box;
	padding: 12px;
	background: var(--surface-1);
	border: 1px solid var(--border);
	border-radius: 11px;
}
.jv-card-title {
	display: block;
	font-size: 13.5px;
	font-weight: 600;
	color: var(--text);
	margin-bottom: 2px;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
/* --link, not --cta — same reason as ReceiptChip's .jv-receipt-link: this <a>
   opens the referenced ERPNext document, and --cta is near-black, which left the
   title indistinguishable from the surrounding --text labels. */
.jv-card-link {
	color: var(--link);
	text-decoration: none;
}
.jv-card-link:hover {
	text-decoration: underline;
}
.jv-card-sub {
	font-size: 11.5px;
	color: var(--text-3);
	margin-bottom: 9px;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-card-field {
	display: flex;
	justify-content: space-between;
	gap: 8px;
	padding: 4px 0;
	border-top: 1px solid var(--border);
	font-size: 12px;
}
.jv-card-field:first-of-type {
	border-top: 0;
}
.jv-card-k {
	color: var(--text-3);
	flex: none;
}
.jv-card-v {
	color: var(--text);
	font-weight: 500;
	text-align: right;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
/* reusable toast notifier (delete confirmations, "no data", any status) */
.jv-notes {
	position: absolute;
	top: 16px;
	left: 50%;
	transform: translateX(-50%);
	z-index: 90;
	display: flex;
	flex-direction: column;
	gap: 8px;
	align-items: center;
	pointer-events: none;
}
.jv-note {
	pointer-events: auto;
	display: flex;
	align-items: center;
	gap: 10px;
	min-width: 220px;
	max-width: 400px;
	padding: 10px 12px;
	background: var(--surface);
	border: 1px solid var(--border-2);
	border-radius: 11px;
	box-shadow: 0 10px 30px rgba(20, 20, 30, 0.18);
}
.jv-note-ic {
	flex: none;
	width: 22px;
	height: 22px;
	border-radius: 50%;
	display: flex;
	align-items: center;
	justify-content: center;
	color: #fff;
}
.jv-note.success .jv-note-ic {
	background: var(--green);
}
.jv-note.error .jv-note-ic {
	background: var(--red);
}
.jv-note.info .jv-note-ic {
	background: var(--cta);
	color: var(--cta-fg);
}
.jv-note-body {
	min-width: 0;
	flex: 1;
}
.jv-note-title {
	font-size: 12px;
	font-weight: 650;
	color: var(--text);
}
.jv-note-msg {
	font-size: 13px;
	color: var(--text);
}
.jv-note-x {
	flex: none;
	width: 24px;
	height: 24px;
	display: flex;
	align-items: center;
	justify-content: center;
	background: transparent;
	border: none;
	border-radius: 6px;
	color: var(--text-3);
	cursor: pointer;
}
.jv-note-x:hover {
	background: var(--surface-2);
	color: var(--text);
}
.jv-note-enter-active,
.jv-note-leave-active {
	transition: opacity 0.18s ease, transform 0.18s ease;
}
.jv-note-enter-from,
.jv-note-leave-to {
	opacity: 0;
	transform: translateY(-8px);
}

/* confirm dialog styles moved to components/shell/ConfirmDialog.vue */

/* proactive message toast */
.jv-toast {
	position: absolute;
	right: 20px;
	bottom: 22px;
	z-index: 70;
	display: flex;
	align-items: center;
	gap: 11px;
	width: 360px;
	max-width: calc(100% - 40px);
	padding: 13px 14px;
	background: var(--surface);
	border: 1px solid var(--border-2);
	border-radius: 13px;
	box-shadow: 0 12px 32px rgba(20, 20, 30, 0.22);
	cursor: pointer;
}
/* .jv-toast-ic removed — the toast icon is now <JarvisMark :size="30" :radius="8" />,
   which owns its own size, radius and gradient. */
.jv-toast-title {
	font-size: 13px;
	font-weight: 600;
	color: var(--text);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-toast-preview {
	font-size: 12px;
	color: var(--text-3);
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.jv-toast-open {
	flex: none;
	padding: 6px 12px;
	background: var(--cta);
	border: 1px solid var(--cta);
	border-radius: 7px;
	font-family: inherit;
	font-size: 12px;
	font-weight: 600;
	color: var(--cta-fg);
	cursor: pointer;
}
.jv-toast-x {
	flex: none;
	width: 26px;
	height: 26px;
	display: flex;
	align-items: center;
	justify-content: center;
	background: transparent;
	border: none;
	border-radius: 6px;
	color: var(--text-3);
	cursor: pointer;
}
.jv-toast-x:hover {
	background: var(--surface-2);
	color: var(--text);
}

/* ===== Macros ===== */
/* list rows */
.jv-macro-sub {
	display: flex;
	align-items: center;
	gap: 8px;
	font-size: 12px;
	color: var(--text-3);
	margin-top: 2px;
}
/* progress banner */
.jv-macrobar {
	display: flex;
	align-items: center;
	gap: 10px;
	padding: 10px 14px;
	border: 1px solid var(--cta);
	background: var(--cta-bg);
	border-radius: 11px;
}
.jv-macrobar.ok {
	border-color: var(--green);
	background: var(--green-bg);
}
.jv-macrobar.err {
	border-color: var(--red-bd);
	background: var(--red-bg);
}
.jv-macrobar.stopped {
	border-color: var(--border-2);
	background: var(--surface-1);
}
.jv-macrobar-dot {
	width: 11px;
	height: 11px;
	flex: none;
	border-radius: 99px;
	background: var(--cta);
}
.jv-macrobar-dot.spin {
	border: 2px solid var(--border-2);
	border-top-color: var(--cta);
	background: transparent;
	animation: jv-spin 0.7s linear infinite;
}
.jv-macrobar-txt {
	flex: 1;
	min-width: 0;
	font-size: 13px;
	font-weight: 550;
	color: var(--text);
}
.jv-macrobar-stop {
	flex: none;
	padding: 6px 14px;
	background: var(--surface);
	border: 1px solid var(--border-2);
	border-radius: 7px;
	font-family: inherit;
	font-size: 12.5px;
	font-weight: 600;
	color: var(--text-2);
	cursor: pointer;
}
.jv-macrobar-stop:hover {
	background: var(--red);
	border-color: var(--red);
	color: #fff;
}
.jv-macrobar-chip {
	font-size: 13px;
	font-weight: 600;
	color: var(--text);
}
/* save-as-macro card (in a message) */
.jv-macrocard {
	display: flex;
	align-items: center;
	gap: 11px;
	margin-top: 12px;
	padding: 11px 12px;
	border: 1px solid var(--border);
	background: var(--surface-1);
	border-radius: 11px;
}
.jv-macrocard-ic {
	flex: none;
	width: 34px;
	height: 34px;
	border-radius: 8px;
	display: flex;
	align-items: center;
	justify-content: center;
	background: var(--cta-bg);
	color: var(--cta);
}
.jv-macrocard-txt {
	flex: 1;
	min-width: 0;
	display: flex;
	flex-direction: column;
	gap: 1px;
}
.jv-macrocard-title {
	font-size: 13px;
	font-weight: 600;
	color: var(--text);
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
}
.jv-macrocard-sub {
	font-size: 11.5px;
	color: var(--text-3);
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
}
.jv-macrocard-btn {
	flex: none;
	padding: 7px 13px;
	background: var(--cta);
	border: 1px solid var(--cta);
	border-radius: 8px;
	font-family: inherit;
	font-size: 12.5px;
	font-weight: 600;
	color: var(--cta-fg);
	cursor: pointer;
	transition: opacity 0.12s;
}
.jv-macrocard-btn:hover {
	opacity: 0.9;
}
.jv-macro-merged-badge {
	margin-left: 7px;
	font-size: 9.5px;
	font-weight: 650;
	letter-spacing: 0.05em;
	text-transform: uppercase;
	color: var(--green);
	background: var(--green-bg);
	border: 1px solid var(--green-bd);
	border-radius: 99px;
	padding: 1px 7px;
}
.jv-macro-merged-badge--pending {
	color: var(--amber);
	background: var(--amber-bg);
	border-color: var(--amber-bd);
}

/* rich action cards (doc confirm / email draft) */
/* .jv-action must stay overflow:visible — the edit form's Link dropdown
   (.jv-action-linkmenu, position:absolute) would be CLIPPED to the card
   otherwise, leaving a sliver you have to scroll inside. The rounded corners
   are preserved by rounding the footer's own bottom edge instead. */
.jv-action,
.jv-email {
	margin-top: 12px;
	border: 1px solid var(--border);
	border-radius: 11px;
	background: var(--surface);
}
.jv-email {
	overflow: hidden;
}
.jv-action-foot {
	border-radius: 0 0 10px 10px;
}
.jv-action-head {
	display: flex;
	align-items: center;
	gap: 8px;
	padding: 11px 14px;
	border-bottom: 1px solid var(--border);
}
.jv-action-head svg {
	flex: none;
}
.jv-action-title {
	font-size: 13px;
	font-weight: 600;
	color: var(--text);
}
.jv-action-title b {
	font-weight: 700;
}
.jv-action-fields {
	padding: 4px 0;
}
.jv-action-row {
	display: flex;
	gap: 12px;
	padding: 6px 14px;
	font-size: 13px;
}
.jv-action-row:not(:last-child) {
	border-bottom: 1px solid var(--surface-2);
}
.jv-action-k {
	flex: none;
	width: 140px;
	color: var(--text-3);
	font-family: ui-monospace, Menlo, monospace;
	font-size: 12px;
}
.jv-action-v {
	flex: 1;
	min-width: 0;
	color: var(--text);
	word-break: break-word;
}
.jv-action-editrow {
	display: flex;
	align-items: flex-start;
	gap: 12px;
	padding: 7px 14px;
}
.jv-action-editrow > .jv-action-k {
	padding-top: 7px;
}
.jv-action-ctl {
	flex: 1;
	min-width: 0;
	position: relative;
}
.jv-action-input {
	width: 100%;
	box-sizing: border-box;
	font-family: inherit;
	font-size: 13px;
	line-height: 1.45;
	color: var(--text);
	background: var(--surface);
	border: 1px solid var(--border-2);
	border-radius: 7px;
	padding: 6px 9px;
	outline: none;
	resize: vertical;
	transition: border-color 0.12s, box-shadow 0.12s;
}
.jv-action-input:focus {
	border-color: var(--cta);
	box-shadow: 0 0 0 3px var(--cta-bg);
}
.jv-action-sel {
	cursor: pointer;
	appearance: auto;
}
.jv-action-link {
	position: relative;
}
.jv-action-linkmenu {
	position: absolute;
	left: 0;
	right: 0;
	top: calc(100% + 4px);
	z-index: 20;
	background: var(--surface);
	border: 1px solid var(--border-2);
	border-radius: 9px;
	box-shadow: 0 8px 24px rgba(20, 20, 30, 0.14);
	padding: 4px;
	max-height: 220px;
	overflow-y: auto;
}
.jv-action-linkmenu.up {
	top: auto;
	bottom: calc(100% + 4px);
	box-shadow: 0 -8px 24px rgba(20, 20, 30, 0.14);
}
.jv-action-linkmenu button {
	display: block;
	width: 100%;
	text-align: left;
	padding: 7px 9px;
	background: transparent;
	border: none;
	border-radius: 6px;
	font-family: inherit;
	font-size: 12.5px;
	color: var(--text-2);
	cursor: pointer;
}
.jv-action-linkmenu button:hover {
	background: var(--surface-2);
	color: var(--text);
}
.jv-action-editrow.changed .jv-action-input {
	border-color: var(--cta);
}
.jv-action-editrow.changed > .jv-action-k {
	color: var(--cta);
	font-weight: 600;
}
.jv-action-edithint {
	padding: 2px 14px 8px;
	font-size: 11.5px;
	color: var(--text-3);
	line-height: 1.4;
}
.jv-action-foot {
	display: flex;
	align-items: center;
	gap: 8px;
	padding: 11px 14px;
	border-top: 1px solid var(--border);
	background: var(--surface-1);
}
.jv-action-primary {
	display: inline-flex;
	align-items: center;
	gap: 7px;
	font-family: inherit;
	font-size: 13px;
	font-weight: 600;
	padding: 8px 14px;
	border-radius: 8px;
	cursor: pointer;
	background: var(--cta);
	color: var(--cta-fg);
	border: 1px solid var(--cta);
}
.jv-action-primary:hover {
	background: var(--text);
	color: var(--surface);
	border-color: var(--text);
}
.jv-action-2nd {
	display: inline-flex;
	align-items: center;
	gap: 7px;
	font-family: inherit;
	font-size: 13px;
	font-weight: 550;
	padding: 8px 13px;
	border-radius: 8px;
	cursor: pointer;
	background: var(--surface);
	color: var(--text-2);
	border: 1px solid var(--border-2);
}
.jv-action-2nd:hover {
	background: var(--text);
	color: var(--surface);
	border-color: var(--text);
}
/* Dark mode: a black hover is invisible on the dark surface and the invert
   would flash a stark white button, so neutral buttons get a subtle elevated
   grey hover and primaries keep their colour (just brighter). */
.jv-dark .jv-iconbtn:hover,
.jv-dark .jv-artifact-head .jv-iconbtn:hover,
.jv-dark .jv-modelpill:hover,
.jv-dark .jv-confirm-no:hover,
.jv-dark .jv-action-2nd:hover {
	background: var(--surface-3) !important;
	color: var(--text) !important;
	border-color: var(--border-2) !important;
}
.jv-dark .jv-iconbtn:hover svg,
.jv-dark .jv-modelpill:hover svg {
	stroke: var(--text) !important;
}
.jv-dark .jv-modelpill:hover span {
	color: var(--text) !important;
}
.jv-dark .jv-confirm-yes:hover,
.jv-dark .jv-action-primary:hover {
	background: var(--cta) !important;
	color: var(--cta-fg) !important;
	border-color: var(--cta) !important;
	filter: brightness(1.18);
}
.jv-action-discard {
	margin-left: auto;
	display: inline-flex;
	align-items: center;
	gap: 6px;
	font-family: inherit;
	font-size: 12.5px;
	font-weight: 600;
	padding: 8px 13px;
	border-radius: 8px;
	border: 1px solid var(--red-bd);
	background: var(--red-bg);
	color: var(--red);
	cursor: pointer;
	transition: background 0.12s, color 0.12s, border-color 0.12s;
}
.jv-action-discard:hover {
	background: var(--red);
	color: #fff;
	border-color: var(--red);
}
.jv-action-discard:hover svg {
	stroke: #fff;
}
/* action:pending confirm card (write-safety gate, issue #186): an amber accent
   marks a write parked awaiting the owner's Confirm click. */
.jv-pending {
	border-color: var(--amber);
}
.jv-pending .jv-action-head {
	border-bottom-color: var(--border);
}
.jv-pending-body {
	padding: 11px 14px;
	display: flex;
	flex-direction: column;
	gap: 8px;
}
.jv-pending-summary {
	font-size: 13.5px;
	line-height: 1.5;
	color: var(--text);
}
.jv-pending-note {
	font-size: 12px;
	line-height: 1.45;
	color: var(--text-3);
}
.jv-pending-expired {
	padding: 9px 11px;
	background: var(--surface-2);
	border: 1px solid var(--border);
	border-radius: 7px;
	color: var(--text-3);
	font-size: 12.5px;
	line-height: 1.45;
}
.jv-pending-preview {
	margin: 0;
	padding: 9px 11px;
	background: var(--surface-2);
	border: 1px solid var(--border);
	border-radius: 7px;
	font-family: ui-monospace, "SF Mono", Menlo, monospace;
	font-size: 11.5px;
	line-height: 1.5;
	color: var(--text);
	white-space: pre-wrap;
	word-break: break-word;
	max-height: 260px;
	overflow-y: auto;
}
.jv-pending-batch {
	margin: 0 14px 8px;
	padding-left: 18px;
	font-size: 12.5px;
	color: var(--text-2);
}
.jv-pending-batch li {
	margin: 2px 0;
}
.jv-action-primary:disabled,
.jv-action-discard:disabled {
	opacity: 0.55;
	cursor: default;
}
/* rollout-window note shown for a legacy gated-write / email card whose own
   action button was removed (issue #186, #12/#13). */
.jv-legacy-note {
	margin-top: 10px;
	padding: 9px 12px;
	border: 1px solid var(--amber-bd);
	background: var(--amber-bg);
	border-radius: 9px;
	font-size: 12.5px;
	line-height: 1.45;
	color: var(--text-2);
}
.jv-email-head {
	padding: 12px 14px 6px;
}
.jv-email-line {
	display: flex;
	gap: 10px;
	font-size: 13px;
	padding: 2px 0;
}
.jv-email-k {
	flex: none;
	width: 54px;
	color: var(--text-3);
}
.jv-email-v {
	color: var(--text-2);
	word-break: break-word;
}
.jv-email-subj {
	color: var(--text);
	font-weight: 600;
}
.jv-email-body {
	padding: 12px 14px 14px;
	font-size: 13px;
	line-height: 1.6;
	color: var(--text);
	white-space: pre-wrap;
	word-break: break-word;
	border-top: 1px solid var(--surface-2);
}

/* --- summary-first confirmation card (Task 1.3) --- */
.jv-summary {
	margin-top: 12px;
	border-color: var(--cta-bd);
}
.jv-summary-body {
	padding: 11px 14px;
	display: flex;
	flex-direction: column;
	gap: 10px;
}
.jv-summary-headline {
	font-size: 13.5px;
	font-weight: 600;
	color: var(--text);
}
.jv-summary-fields {
	display: grid;
	grid-template-columns: max-content 1fr;
	gap: 4px 14px;
	margin: 0;
}
.jv-summary-fields dt {
	font-size: 10.5px;
	font-weight: 650;
	letter-spacing: 0.06em;
	text-transform: uppercase;
	color: var(--text-3);
	align-self: center;
}
.jv-summary-fields dd {
	margin: 0;
	font-size: 13.5px;
	color: var(--text);
}
.jv-summary-diffrow {
	display: flex;
	align-items: baseline;
	gap: 8px;
	font-size: 13px;
	padding: 3px 0;
	flex-wrap: wrap;
}
.jv-summary-difflbl {
	font-weight: 600;
	color: var(--text-2);
	min-width: 120px;
}
.jv-summary-from {
	color: var(--text-3);
	text-decoration: line-through;
}
.jv-summary-arrow {
	color: var(--text-3);
}
.jv-summary-to {
	color: var(--green);
	font-weight: 600;
}
.jv-summary-nochange,
.jv-summary-loading {
	font-size: 12.5px;
	color: var(--text-3);
}
/* A draft that could not be built. Deliberately NOT jv-summary-loading's muted
   grey: this is a dead end, not a pending state, and it read as a placeholder. */
.jv-summary-err {
	display: flex;
	align-items: flex-start;
	gap: 8px;
	font-size: 12.5px;
	color: var(--red);
	line-height: 1.45;
}
.jv-summary-err svg {
	flex: none;
	margin-top: 1px;
}
.jv-summary-table {
	border: 1px solid var(--border);
	border-radius: 8px;
	overflow: hidden;
}
.jv-summary-table-h {
	padding: 7px 10px;
	background: var(--surface-2);
	font-size: 12px;
	font-weight: 650;
	color: var(--text-2);
	border-bottom: 1px solid var(--border);
}
.jv-summary-gridwrap {
	overflow-x: auto;
}
.jv-summary-grid {
	width: 100%;
	border-collapse: collapse;
	font-size: 12.5px;
}
.jv-summary-grid th {
	text-align: left;
	padding: 6px 10px;
	color: var(--text-3);
	font-weight: 600;
	font-size: 10.5px;
	letter-spacing: 0.05em;
	text-transform: uppercase;
	white-space: nowrap;
	background: var(--surface-1);
	border-bottom: 1px solid var(--border);
}
.jv-summary-grid td {
	padding: 5px 10px;
	color: var(--text);
	white-space: nowrap;
	border-bottom: 1px solid var(--border);
}
.jv-summary-grid tbody tr:last-child td {
	border-bottom: none;
}
.jv-summary-hint {
	padding: 0 14px 11px;
	font-size: 12px;
	color: var(--text-3);
}

/* --- record draft panel --- */
.jv-draft-panel {
	position: relative;
	max-width: 92vw;
	display: flex;
	flex-direction: column;
}
/* Drag handle on the inner (left) edge; 1px rule that lights up on hover/drag. */
.jv-draft-resizer {
	position: absolute;
	left: 0;
	top: 0;
	z-index: 3;
	height: 100%;
	width: 8px;
	margin-left: -4px;
	cursor: col-resize;
}
.jv-draft-resizer::before {
	content: "";
	position: absolute;
	left: 4px;
	top: 0;
	height: 100%;
	width: 1px;
	background: var(--cta);
	opacity: 0;
	transition: opacity 0.15s ease;
}
.jv-draft-resizer:hover::before,
.jv-draft-resizer-active::before {
	opacity: 1;
}
.jv-draft-panel-resizing {
	user-select: none;
}
:global(body.jv-col-resizing) {
	cursor: col-resize;
	user-select: none;
}
.jv-draft-badge {
	font-size: 10px;
	font-weight: 650;
	letter-spacing: 0.08em;
	text-transform: uppercase;
	color: var(--amber);
	background: var(--amber-bg);
	border: 1px solid var(--amber-bd);
	border-radius: 99px;
	padding: 3px 9px;
	margin-left: 8px;
}
.jv-draft-body {
	flex: 1;
	overflow-y: auto;
	padding: 14px 16px;
	display: flex;
	flex-direction: column;
	gap: 14px;
}
.jv-draft-toast {
	font-size: 12px;
	color: var(--cta);
	background: var(--cta-bg);
	border: 1px solid var(--cta-bd);
	border-radius: 8px;
	padding: 7px 11px;
}
.jv-draft-fields {
	display: grid;
	grid-template-columns: 1fr 1fr;
	gap: 10px 12px;
}
.jv-draft-fld label {
	display: block;
	font-size: 10.5px;
	font-weight: 650;
	letter-spacing: 0.06em;
	text-transform: uppercase;
	color: var(--text-3);
	margin-bottom: 4px;
}
.jv-draft-fld .jv-req {
	color: var(--red);
}
.jv-draft-fld.missing .jv-action-input {
	border-color: var(--amber-bd);
}
.jv-draft-fld.changed .jv-action-input {
	border-color: var(--cta-bd);
	background: var(--cta-bg);
}
.jv-draft-ctl {
	position: relative;
}
.jv-draft-table-title {
	font-size: 12px;
	font-weight: 650;
	color: var(--text-2);
	margin-bottom: 6px;
}
.jv-draft-gridwrap {
	overflow-x: auto;
	border: 1px solid var(--border);
	border-radius: 9px;
}
.jv-grid {
	width: 100%;
	border-collapse: collapse;
	font-size: 13px;
}
.jv-grid th {
	font-size: 10.5px;
	font-weight: 650;
	letter-spacing: 0.05em;
	text-transform: uppercase;
	color: var(--text-3);
	text-align: left;
	padding: 7px 8px;
	border-bottom: 1px solid var(--border-2);
	background: var(--surface-1);
}
.jv-grid td {
	padding: 5px 6px;
	border-bottom: 1px solid var(--border);
	position: relative;
	min-width: 90px;
}
.jv-grid td .jv-action-input {
	width: 100%;
	box-sizing: border-box;
}
.jv-grid-ro {
	color: var(--text-3);
}
.jv-grid-x {
	width: 30px;
}
.jv-grid-del {
	background: none;
	border: none;
	color: var(--text-3);
	cursor: pointer;
	padding: 4px 6px;
	border-radius: 6px;
}
.jv-grid-del:hover {
	color: var(--red);
	background: var(--red-bg);
}
.jv-draft-addrow {
	align-self: flex-start;
	margin-top: 8px;
	background: none;
	border: none;
	color: var(--cta);
	font-weight: 600;
	font-size: 12.5px;
	cursor: pointer;
	padding: 4px 2px;
}
.jv-draft-totals {
	display: flex;
	flex-wrap: wrap;
	align-items: flex-end;
	gap: 10px 14px;
	padding: 12px 14px;
	border: 1px solid var(--border);
	border-radius: 10px;
	background: var(--surface-1);
}
.jv-draft-total-fld {
	display: flex;
	flex-direction: column;
	flex: 1 1 130px;
	min-width: 120px;
}
.jv-draft-total-fld label {
	display: block;
	font-size: 10.5px;
	font-weight: 650;
	letter-spacing: 0.06em;
	text-transform: uppercase;
	color: var(--text-3);
	margin-bottom: 4px;
}
.jv-draft-total-ctl {
	position: relative;
}
/* read-only: muted fill, no caret, a lock glyph — clearly not an editable field */
.jv-draft-total-input {
	width: 100%;
	box-sizing: border-box;
	background: var(--surface-2);
	color: var(--text);
	font-weight: 650;
	font-variant-numeric: tabular-nums;
	cursor: default;
	padding-right: 28px;
}
.jv-draft-total-input:focus {
	border-color: var(--border-2);
	box-shadow: none;
}
.jv-draft-total-lock {
	position: absolute;
	right: 9px;
	top: 50%;
	transform: translateY(-50%);
	color: var(--text-3);
	pointer-events: none;
}
.jv-draft-est {
	flex-basis: 100%;
	margin: 0;
	color: var(--text-3);
	font-size: 11px;
}
.jv-draft-foot {
	display: flex;
	gap: 10px;
	align-items: center;
	padding: 12px 16px;
	border-top: 1px solid var(--border);
}
@media (max-width: 700px) {
	.jv-draft-fields {
		grid-template-columns: 1fr;
	}
}

/* artifact preview panel (right side-over) */
/* premium artifact-preview header actions (replaces the boxy .jv-iconbtn look) */
.jv-art-act,
.jv-art-close {
	flex: none;
	width: 32px;
	height: 32px;
	display: inline-flex;
	align-items: center;
	justify-content: center;
	border-radius: 9px;
	border: 1px solid transparent;
	background: transparent;
	color: var(--text-3);
	cursor: pointer;
	text-decoration: none;
	transition: background 0.15s ease, color 0.15s ease, border-color 0.15s ease,
		transform 0.15s ease, box-shadow 0.15s ease;
}
.jv-art-act svg,
.jv-art-close svg {
	transition: transform 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
}
.jv-art-act:hover {
	background: var(--surface-2);
	border-color: var(--border);
	color: var(--text);
	transform: translateY(-1px);
	box-shadow: 0 4px 12px rgba(20, 20, 30, 0.1);
}
.jv-art-act:active {
	transform: translateY(0) scale(0.94);
}
.jv-art-divider {
	flex: none;
	width: 1px;
	height: 18px;
	margin: 0 3px;
	background: var(--border);
	border-radius: 1px;
}
.jv-art-close {
	border-radius: 50%;
}
.jv-art-close:hover {
	background: var(--red-bg);
	border-color: var(--red-bd);
	color: var(--red);
	box-shadow: 0 4px 12px rgba(220, 38, 38, 0.14);
}
.jv-art-close:hover svg {
	transform: rotate(90deg);
}
.jv-art-close:active {
	transform: scale(0.9);
}
.jv-artifact-panel:focus {
	outline: none;
}
.jv-artifact-overlay {
	position: absolute;
	inset: 0;
	z-index: 60;
	background: rgba(15, 15, 22, 0.32);
	display: flex;
	justify-content: flex-end;
}
.jv-dark .jv-artifact-overlay {
	background: rgba(0, 0, 0, 0.5);
}
.jv-artifact-panel {
	width: min(720px, 82%);
	height: 100%;
	background: var(--surface);
	border-left: 1px solid var(--border);
	display: flex;
	flex-direction: column;
	box-shadow: -14px 0 44px rgba(20, 20, 30, 0.14);
}
.jv-artifact-head {
	display: flex;
	align-items: center;
	gap: 9px;
	padding: 11px 12px 11px 14px;
	border-bottom: 1px solid var(--border);
	flex: none;
}
.jv-artifact-head svg {
	color: var(--text-3);
	flex: none;
}
.jv-artifact-head-title {
	font-size: 14px;
	font-weight: 600;
	color: var(--text);
	white-space: nowrap;
	overflow: hidden;
	text-overflow: ellipsis;
	flex: 1;
	min-width: 0;
}
.jv-artifact-head .jv-iconbtn:hover {
	background: var(--text) !important;
	color: var(--surface) !important;
}
.jv-artifact-body {
	flex: 1;
	min-height: 0;
	overflow: auto;
	background: var(--surface-1);
	display: flex;
	flex-direction: column;
}
.jv-artifact-frame {
	flex: 1;
	width: 100%;
	border: 0;
	background: #fff;
}
/* Dark preview: the frame behind html/svg srcdoc follows the app surface (the
   srcdoc itself is themed by get_canvas's dark param); PDFs keep the white
   frame — pages are white paper either way. */
.jv-dark .jv-artifact-frame:not([title="PDF preview"]) {
	background: var(--surface-1);
}
.jv-artifact-img {
	max-width: 100%;
	height: auto;
	margin: auto;
	padding: 16px;
}
.jv-artifact-text {
	margin: 0;
	padding: 16px;
	font-size: 12.5px;
	line-height: 1.55;
	white-space: pre-wrap;
	word-break: break-word;
	color: var(--text);
	font-family: ui-monospace, "SF Mono", Menlo, monospace;
}
.jv-artifact-loading,
.jv-artifact-nopreview {
	margin: auto;
	padding: 30px;
	text-align: center;
	color: var(--text-3);
	font-size: 13px;
	display: flex;
	flex-direction: column;
	gap: 12px;
	align-items: center;
}
.jv-sheet-tabs {
	display: flex;
	gap: 4px;
	padding: 8px 10px;
	border-bottom: 1px solid var(--border);
	background: var(--surface);
	flex: none;
	overflow-x: auto;
}
.jv-sheet-tabs button {
	font-family: inherit;
	font-size: 12px;
	padding: 4px 10px;
	border: 1px solid var(--border);
	border-radius: 6px;
	background: var(--surface-1);
	color: var(--text-2);
	cursor: pointer;
	white-space: nowrap;
}
.jv-sheet-tabs button.on {
	background: var(--cta);
	color: var(--cta-fg);
	border-color: var(--cta);
}
.jv-sheet-scroll {
	flex: 1;
	min-height: 0;
	overflow: auto;
}
.jv-sheet {
	border-collapse: collapse;
	font-size: 12.5px;
	width: max-content;
	min-width: 100%;
}
.jv-sheet th,
.jv-sheet td {
	border: 1px solid var(--border);
	padding: 6px 10px;
	text-align: left;
	white-space: nowrap;
}
.jv-sheet th {
	background: var(--surface-2);
	font-weight: 600;
	color: var(--text);
	position: sticky;
	top: 0;
	z-index: 1;
}
.jv-sheet td {
	color: var(--text-2);
}
.jv-sheet tbody tr:nth-child(even) td {
	background: var(--surface);
}

.jv-slide-enter-active,
.jv-slide-leave-active {
	transition: opacity 0.18s ease;
}
.jv-slide-enter-active .jv-artifact-panel,
.jv-slide-leave-active .jv-artifact-panel {
	transition: transform 0.24s cubic-bezier(0.4, 0, 0.2, 1);
}
.jv-slide-enter-from,
.jv-slide-leave-to {
	opacity: 0;
}
.jv-slide-enter-from .jv-artifact-panel,
.jv-slide-leave-to .jv-artifact-panel {
	transform: translateX(100%);
}
</style>
