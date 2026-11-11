<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { beforeNavigate } from '$app/navigation';
	import { api, ApiError, errorText, setCsrf, type Session, type Turn } from '#lib/api.js';
	import ResponseMessage from '#lib/components/chat/Messages/ResponseMessage.svelte';
	import Reference from '#lib/components/review/Reference.svelte';
	import Evidence from '#lib/components/review/Evidence.svelte';
	import ServiceNav from '#lib/components/common/ServiceNav.svelte';

	type RatingStatus = 'pending' | 'scored' | 'not_applicable';
	type Score = { score: number | null; status: RatingStatus; rationale: string };
	type Entry = {
		dimension: string;
		rubric: {
			title: string;
			description: string;
			levels: Record<string, string>;
			not_applicable: string;
		};
		scale: number[];
		scale_labels: Record<string, string>;
		forced_na: boolean;
		inputs: { question: string; history: { role: string; content: string }[] };
		outputs: NonNullable<Turn['result']> & {
			generation_sources?: Record<string, unknown>[];
			agent_steps?: unknown[];
		};
		expectations?: Record<string, unknown>;
	};
	type ReviewCase = {
		record_id: string;
		answer_digest: string;
		position: number;
		total: number;
		dimensions: Entry[];
		saved: {
			synced: boolean;
			ratings: ({ dimension: string } & Score)[];
		} | null;
		draft: { scores: Record<string, Score>; revision: number; updated_at: string } | null;
	};
	type Round = {
		grading_id: string;
		rubric_digest: string;
		rubric_id: string;
		grading_run_id: string | null;
		grading_state: string;
		run_id: string;
		round: number;
		dataset_id: string;
		kind: string;
		total: number;
		completed: number;
		synced: number;
		next_position: number;
		created_at: string;
	};
	type Candidate = {
		run_id: string;
		run_name?: string;
		status: string;
		start_time?: number;
		dataset_id: string;
		dataset_name: string;
		url?: string;
	};
	type Progress = { position: number; submitted: boolean; synced: boolean; draft: boolean };
	let session: Session | null = null,
		ready = false,
		busy = false,
		error = '',
		notice = '';
	let candidates: Candidate[] = [],
		rounds: Round[] = [],
		progress: Progress[] = [];
	let nextPage: string | null = null,
		query = '',
		listError = '';
	let run = '',
		number = 0,
		gradingId = '',
		current: ReviewCase | null = null,
		active = '';
	let origin: Candidate | null = null,
		originError = '';
	let gradingOrigin: Candidate | null = null;
	let scores: Record<string, Score> = {},
		draftSnapshot = '',
		draftRevision = 0;
	let draftState: 'idle' | 'pending' | 'saving' | 'saved' | 'error' = 'idle',
		draftError = '',
		draftBlocked = false;
	let draftTimer: ReturnType<typeof setTimeout> | undefined,
		draftFlight: Promise<void> | null = null;
	let questionHeading: HTMLHeadingElement;
	let answerPanel: HTMLElement;
	const names: Record<string, string> = {
		correctness: '정확성',
		completeness: '완전성',
		groundedness: '근거 충실도',
		relevance: '응답 적절성',
		behavior: '행동 준수'
	};
	const refNames: Record<string, string> = {
		answerable: '답변 가능 여부',
		answer: '모범 답안',
		required_points: '필수 항목',
		unacceptable_claims: '허용하지 않는 주장',
		evidence: '검증된 참고 근거',
		expected_behavior: '기대 행동',
		failure_conditions: '실패 조건'
	};
	const statuses: Record<string, string> = {
		FINISHED: '실행 완료',
		FAILED: '일부 실패',
		KILLED: '중단됨',
		RUNNING: '실행 중',
		SCHEDULED: '실행 대기'
	};

	function date(value?: number | string) {
		return value
			? new Date(value).toLocaleString('ko-KR', {
					month: 'short',
					day: 'numeric',
					hour: '2-digit',
					minute: '2-digit'
				})
			: '';
	}
	function safeUrl(value: unknown) {
		if (typeof value !== 'string') return undefined;
		try {
			const u = new URL(value);
			return ['http:', 'https:'].includes(u.protocol) ? u.href : undefined;
		} catch {
			return undefined;
		}
	}
	function own(id: string, records: Round[]) {
		return records.filter((r) => r.run_id === id);
	}
	function done(id: string, records: Round[]) {
		return own(id, records).filter((r) => r.total > 0 && r.completed === r.total).length;
	}
	function filled(key: string, values: Record<string, Score>) {
		return (
			(values[key]?.status === 'not_applicable' ||
				(values[key]?.status === 'scored' && values[key]?.score !== null)) &&
			!!values[key]?.rationale.trim()
		);
	}
	function path() {
		return `/reviews/${run}/gradings/${gradingId}`;
	}
	function evaluations(available: Candidate[], records: Round[]) {
		const items = [...available];
		for (const r of records)
			if (!items.some((c) => c.run_id === r.run_id))
				items.push({
					run_id: r.run_id,
					dataset_id: r.dataset_id,
					dataset_name: r.kind === 'gold' ? '🥇 Gold Set' : '🛡️ Iron Set',
					status: 'FINISHED'
				});
		return items;
	}
	$: selected = rounds.find((r) => r.grading_id === gradingId);
	$: runRounds = rounds.filter((r) => r.run_id === run);
	$: available = evaluations(candidates, rounds);
	$: visible = available.filter((c) =>
		`${c.dataset_name} ${c.run_name ?? ''} ${c.run_id}`.toLowerCase().includes(query.toLowerCase())
	);
	$: entry = current?.dimensions.find((e) => e.dimension === active);
	$: completeCount = current?.dimensions.filter((e) => filled(e.dimension, scores)).length ?? 0;
	$: complete = !!current && completeCount === current.dimensions.length;
	$: dirty = !!current && !current.saved && JSON.stringify(scores) !== draftSnapshot;
	$: if (ready && dirty && !draftBlocked) scheduleDraft(JSON.stringify(scores));
	$: turn = current
		? ({
				id: `${run}-${number}-${current.record_id}`,
				question: '',
				status: 'completed',
				stage: '',
				error: null,
				result: current.dimensions[0].outputs
			} satisfies Turn)
		: null;

	function scheduleDraft(_text: string) {
		clearTimeout(draftTimer);
		draftState = 'pending';
		draftTimer = setTimeout(() => {
			void saveDraftOnce().catch(() => {});
		}, 700);
	}
	async function saveDraftOnce(): Promise<void> {
		if (draftFlight) return draftFlight;
		if (!current || current.saved || JSON.stringify(scores) === draftSnapshot) return;
		const text = JSON.stringify(scores),
			target = `${path()}/cases/${current.position}/draft`;
		const body = {
			answer_digest: current.answer_digest,
			revision: draftRevision,
			scores: JSON.parse(text)
		};
		draftState = 'saving';
		draftError = '';
		draftFlight = (async () => {
			try {
				const data = await api<{ revision: number }>(target, 'PUT', body);
				draftRevision = data.revision;
				draftSnapshot = text;
				draftState = 'saved';
			} catch (e) {
				clearTimeout(draftTimer);
				draftBlocked = true;
				draftState = 'error';
				draftError = errorText(e);
				throw e;
			} finally {
				draftFlight = null;
			}
		})();
		return draftFlight;
	}
	async function flushDraft() {
		clearTimeout(draftTimer);
		if (draftBlocked) throw new Error('draft_blocked');
		if (draftFlight) await draftFlight;
		while (current && !current.saved && JSON.stringify(scores) !== draftSnapshot)
			await saveDraftOnce();
		clearTimeout(draftTimer);
	}
	async function perform(work: () => Promise<void>) {
		if (busy) return;
		busy = true;
		error = '';
		notice = '';
		try {
			await flushDraft();
			await work();
		} catch (e) {
			if (e instanceof ApiError && e.status === 401) {
				error = '로그인이 만료되었습니다. 입력을 복사한 뒤 다시 로그인해 주세요.';
			} else error = draftBlocked ? draftError : errorText(e);
		} finally {
			busy = false;
		}
	}
	async function refresh() {
		rounds = (await api<{ items: Round[] }>('/reviews')).items;
	}
	async function candidatesPage(token: string | null = null) {
		listError = '';
		try {
			const data = await api<{ items: Candidate[]; next_page_token: string | null }>(
				'/review-candidates' + (token ? `?page_token=${encodeURIComponent(token)}` : '')
			);
			candidates = token ? [...candidates, ...data.items] : data.items;
			nextPage = data.next_page_token;
		} catch (e) {
			listError = errorText(e);
		}
	}
	async function loadProgress() {
		progress = (await api<{ items: Progress[] }>(`${path()}/progress`)).items;
	}
	async function loadCase(position: number) {
		clearTimeout(draftTimer);
		const data = await api<ReviewCase>(`${path()}/cases/${position}`);
		current = data;
		draftRevision = data.draft?.revision ?? 0;
		draftBlocked = false;
		draftError = '';
		scores = Object.fromEntries(
			data.dimensions.map((e) => {
				const saved = data.saved?.ratings.find((s) => s.dimension === e.dimension);
				return [
					e.dimension,
					saved
						? { score: saved.score, status: saved.status, rationale: saved.rationale }
						: (data.draft?.scores[e.dimension] ?? {
								score: null,
								status: e.forced_na ? 'not_applicable' : 'pending',
								rationale: e.forced_na ? '문항/응답에 적용되지 않는 평가 차원' : ''
							})
				];
			})
		);
		draftSnapshot = JSON.stringify(scores);
		draftState = data.draft ? 'saved' : 'idle';
		active =
			data.dimensions.find((e) => !filled(e.dimension, scores))?.dimension ??
			data.dimensions[0].dimension;
		await tick();
		answerPanel?.scrollTo({ top: 0, behavior: 'auto' });
		questionHeading?.focus({ preventScroll: true });
		questionHeading?.scrollIntoView({ block: 'nearest', behavior: 'auto' });
	}
	async function openRound(r: Round) {
		gradingOrigin = null;
		run = r.run_id;
		number = r.round;
		gradingId = r.grading_id;
		current = null;
		draftSnapshot = '{}';
		scores = {};
		progress = [];
		await loadProgress();
		await loadCase(r.next_position);
		await loadGradingOrigin();
	}
	async function loadGradingOrigin() {
		if (session?.identity.role !== 'admin') return;
		try {
			gradingOrigin = await api<Candidate | null>(
				`/admin/reviews/${run}/gradings/${gradingId}/origin`
			);
		} catch {
			gradingOrigin = null;
		}
	}
	async function selectRun(id: string) {
		run = id;
		number = 0;
		current = null;
		scores = {};
		draftSnapshot = '{}';
		progress = [];
		origin = null;
		originError = '';
		const pending = own(id, rounds).find((r) => r.completed < r.total);
		if (pending) await openRound(pending);
		if (session?.identity.role === 'admin') {
			try {
				origin = await api<Candidate>(`/admin/reviews/${id}/origin`);
			} catch {
				originError = '원본 run 연결 정보는 잠시 불러오지 못했습니다.';
			}
		}
	}
	async function startRound() {
		const started = await api<{ run_id: string; round: number; grading_id: string }>(
			'/reviews',
			'POST',
			{
				run_id: run
			}
		);
		await refresh();
		await openRound(rounds.find((r) => r.grading_id === started.grading_id)!);
	}
	async function saveNext() {
		if (!current) return;
		const position = current.position;
		await api(`${path()}/cases/${position}`, 'POST', {
			answer_digest: current.answer_digest,
			scores,
			draft_revision: draftRevision
		});
		// Final submission succeeded even if a subsequent list refresh fails.
		current = {
			...current,
			saved: {
				synced: false,
				ratings: Object.entries(scores).map(([dimension, s]) => ({ dimension, ...s }))
			}
		};
		await refresh();
		await loadProgress();
		const next = progress.find((p) => !p.submitted);
		if (next) {
			await loadCase(next.position);
			notice = `질문 ${position + 1}을 제출했습니다.`;
		} else {
			await loadCase(position);
			notice = `${number}회차의 모든 질문을 제출했습니다.`;
		}
	}
	async function sync() {
		const data = await api<{ synced: number }>(`${path()}/sync`, 'POST');
		await refresh();
		await loadProgress();
		if (current) await loadCase(current.position);
		await loadGradingOrigin();
		notice = data.synced
			? `${number}회차의 질문 ${data.synced}개를 내 채점 run에 동기화했습니다.`
			: '이미 모두 동기화되어 있습니다.';
	}
	async function download() {
		const data = await api<unknown>(`${path()}/result`);
		const url = URL.createObjectURL(
			new Blob([JSON.stringify(data, null, 2) + '\n'], {
				type: 'application/json'
			})
		);
		const link = document.createElement('a');
		link.href = url;
		link.download = `grading-result-${run}-round-${number}.json`;
		link.click();
		URL.revokeObjectURL(url);
	}
	function unload(event: BeforeUnloadEvent) {
		if (current && !current.saved && JSON.stringify(scores) !== draftSnapshot) {
			event.preventDefault();
			event.returnValue = '';
		}
	}
	beforeNavigate(({ cancel }) => {
		if (
			current &&
			!current.saved &&
			JSON.stringify(scores) !== draftSnapshot &&
			!confirm('아직 저장되지 않은 입력이 있습니다. 이 화면을 떠날까요?')
		)
			cancel();
	});
	onMount(() => {
		void perform(async () => {
			session = await api<Session>('/auth/session');
			setCsrf(session.csrf_token);
			await refresh();
			await candidatesPage();
			ready = true;
		});
		return () => clearTimeout(draftTimer);
	});
</script>

<svelte:head><title>Human Review · 엘리스 테크블로그</title></svelte:head>
<svelte:window on:beforeunload={unload} />
<div class="min-h-screen bg-[#f7f8f5] text-gray-900">
	<header class="border-b border-gray-200 bg-white">
		<div class="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-3 px-5 py-4">
			<div class="flex items-center gap-3">
				<img src="/open-webui-logo.png" alt="Open WebUI" class="size-7" /><strong
					>Human Review</strong
				><span class="hidden text-sm text-gray-400 sm:inline">내 계정으로 검수하기</span>
			</div>
			<div class="ml-auto flex flex-wrap items-center justify-end gap-4 text-sm">
				<span class="rounded-full bg-gray-100 px-3 py-1"
					>{session?.identity.label ?? '접속 확인 중'}</span
				>
				<ServiceNav
					active="review"
					admin={session?.identity.role === 'admin'}
					disabled={busy}
					navigate={(href) => {
						void perform(async () => location.assign(href));
					}}
				/>
			</div>
		</div>
	</header>
	<main class="mx-auto max-w-[1600px] px-4 py-7 sm:px-6">
		<div class="mb-6">
			<p class="text-xs tracking-widest text-[#6c7c6b]">EVALUATION · HUMAN REVIEW</p>
			<h1 class="mt-2 text-2xl font-semibold sm:text-3xl">같은 답변을, 독립적인 판단으로.</h1>
			<p class="mt-2 text-sm leading-7 text-gray-500">
				평가 실행은 고정된 답변 묶음입니다. 검수 회차는 그 답변에 대해 내가 한 번 채점하는
				과정입니다.
			</p>
		</div>
		{#if error}<p
				role="alert"
				class="mb-5 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800"
			>
				{error}
			</p>{/if}
		{#if notice}<p
				role="status"
				class="mb-5 rounded-xl border border-green-200 bg-green-50 p-4 text-sm text-green-900"
			>
				{notice}
			</p>{/if}
		{#if !ready}<div class="panel py-16 text-center" role="status">
				{error
					? '로그인과 연결 상태를 확인해 주세요.'
					: '평가 실행과 내 검수 기록을 불러오고 있어요…'}
			</div>
		{:else}<div class="grid items-start gap-6 lg:grid-cols-[270px_minmax(0,1fr)]">
				<aside class="panel space-y-4 lg:sticky lg:top-5">
					<div class="flex items-center justify-between">
						<h2 class="font-semibold">평가 실행</h2>
						<button
							class="text-xs text-[#3f5840] underline"
							disabled={busy}
							on:click={() =>
								perform(async () => {
									await refresh();
									await candidatesPage();
								})}>새로고침</button
						>
					</div>
					<label class="block"
						><span class="sr-only">평가 실행 검색</span><input
							class="input w-full text-sm"
							type="search"
							placeholder="데이터셋·실행 검색"
							bind:value={query}
						/></label
					>
					{#if listError}<p
							role="status"
							class="rounded-lg bg-amber-50 p-3 text-xs leading-6 text-amber-900"
						>
							{listError} 이미 시작한 내 회차는 계속 검수할 수 있습니다.
						</p>{/if}
					<div class="max-h-[65vh] space-y-2 overflow-y-auto">
						{#each visible as candidate}<button
								class="w-full rounded-xl border p-4 text-left transition {run === candidate.run_id
									? 'border-[#81977b] bg-[#eff4eb]'
									: 'border-gray-200 bg-white hover:bg-gray-50'}"
								disabled={busy}
								aria-pressed={run === candidate.run_id}
								on:click={() => perform(() => selectRun(candidate.run_id))}
								><span class="block text-sm font-semibold"
									>{candidate.dataset_name ?? candidate.dataset_id}</span
								><span class="mt-1 block text-xs text-gray-500"
									>{candidate.run_name ?? '평가 실행'}{candidate.start_time
										? ` · ${date(candidate.start_time)}`
										: ''}</span
								><span class="mt-1 block text-[11px] text-gray-400"
									>{statuses[candidate.status] ?? candidate.status} · {candidate.run_id.slice(
										0,
										8
									)}</span
								><span class="mt-3 block text-xs font-medium text-[#3f5840]"
									>내 검수 {done(candidate.run_id, rounds)}회 완료{own(
										candidate.run_id,
										rounds
									).some((r) => r.completed < r.total)
										? ' · 진행 중'
										: ''}</span
								></button
							>{:else}<p class="py-6 text-sm leading-7 text-gray-500">
								{query
									? '검색 결과가 없습니다.'
									: '평가 실행이 없습니다. 먼저 데이터셋 평가를 실행해 답변을 생성해 주세요.'}
							</p>{/each}
					</div>
					{#if nextPage}<button
							class="button w-full"
							disabled={busy}
							on:click={() => perform(() => candidatesPage(nextPage))}>평가 실행 더 보기</button
						>{/if}
				</aside>
				<div class="min-w-0 space-y-5">
					{#if run}
						<section class="panel">
							<div class="flex flex-wrap items-start justify-between gap-4">
								<div>
									<h2 class="text-lg font-semibold">
										{origin?.dataset_name ??
											available.find((c) => c.run_id === run)?.dataset_name ??
											'선택한 평가 실행'}
									</h2>
									<p class="mt-1 text-sm text-gray-500">
										내 검수 <strong class="text-gray-700">{done(run, rounds)}회 완료</strong> · 다른 사람의
										결과와 이전 회차의 점수는 새 회차에 표시하지 않습니다.
									</p>
								</div>
								<button
									class="button primary"
									disabled={busy ||
										!['FINISHED', 'FAILED', 'KILLED'].includes(
											available.find((c) => c.run_id === run)?.status ?? ''
										)}
									on:click={() => perform(startRound)}
									>{own(run, rounds).some((r) => r.completed < r.total)
										? '진행 중인 회차 이어서 검수'
										: own(run, rounds).length
											? '새 회차로 다시 검수'
											: '1회차 검수 시작'}</button
								>
							</div>
							{#if session?.identity.role === 'admin' && safeUrl(origin?.url)}<a
									class="mt-3 inline-block text-xs text-[#3f5840] underline"
									href={safeUrl(origin?.url)}
									target="_blank"
									rel="noreferrer">원본 답변 run 열기 ↗</a
								>{/if}
							{#if runRounds.length}<div
									class="mt-5 flex flex-wrap gap-2"
									aria-label="내 검수 회차"
								>
									{#each runRounds as r}<button
											class="round-button {gradingId === r.grading_id ? 'selected' : ''}"
											disabled={busy}
											aria-pressed={gradingId === r.grading_id}
											on:click={() => perform(() => openRound(r))}
											><strong>{r.round}회차 · {r.rubric_id}</strong><span
												>{r.completed === r.total
													? '검수 완료'
													: `${r.completed}/${r.total}개 제출`}</span
											><span class="text-[10px] text-gray-400">{date(r.created_at)}</span></button
										>{/each}
								</div>{/if}
							{#if session?.identity.role === 'admin'}<details class="mt-4 text-xs text-gray-500">
									<summary class="cursor-pointer">원본 실행 정보</summary>
									<p class="mt-3 break-all font-mono">{run}</p>
									{#if origin}<p class="mt-2">{origin.run_name} · {date(origin.start_time)}</p>
										{#if safeUrl(origin.url)}<a
												class="mt-2 inline-block underline"
												href={safeUrl(origin.url)}
												target="_blank"
												rel="noreferrer">원본 답변 run 열기 ↗</a
											>{/if}
										<p class="mt-2">
											사람 판정과 quality metric은 아래 내 채점 run에 기록됩니다.
										</p>{:else if originError}<p class="mt-2">{originError}</p>{/if}
								</details>{/if}
						</section>
						{#if selected && current && turn}
							<section class="panel">
								<div class="flex flex-wrap items-center justify-between gap-3">
									<div>
										<strong>{number}회차</strong><span class="ml-3 text-sm text-gray-500"
											>{selected.completed}/{selected.total}개 제출 · {selected.synced}개 동기화</span
										>
									</div>
									<div class="flex flex-wrap gap-2">
										<button
											class="button"
											disabled={busy || selected.completed === selected.synced}
											on:click={() => perform(sync)}
											>{!selected.completed
												? '제출 후 동기화'
												: selected.completed === selected.synced
													? 'MLflow 동기화 완료'
													: `MLflow 동기화 · ${selected.completed - selected.synced}개 대기`}</button
										><button
											class="button"
											disabled={busy || !selected.completed}
											on:click={() => perform(download)}>내 판정 내려받기</button
										>
									</div>
								</div>
								<div class="mt-3 text-xs text-gray-500">
									{#if !selected.grading_run_id}채점 run 연결 대기 중입니다. 판정은 로컬에 보존되며
										동기화 때 다시 연결합니다.
									{:else}내 채점 run · {selected.grading_state === 'FINISHED'
											? '완료'
											: selected.grading_state === 'FAILED'
												? '오류'
												: '진행 중'}{/if}
									{#if session?.identity.role === 'admin' && safeUrl(gradingOrigin?.url)}<a
											class="ml-3 text-[#3f5840] underline"
											href={safeUrl(gradingOrigin?.url)}
											target="_blank"
											rel="noreferrer">내 채점 run 열기 ↗</a
										>{/if}
								</div>
								<progress
									class="mt-4 block h-2 w-full accent-[#526b49]"
									value={selected.completed}
									max={selected.total}
									aria-label="현재 회차 검수 진행률"
								></progress>
								<div class="mt-4 flex flex-wrap gap-2" aria-label="질문 이동">
									{#each progress as p}<button
											class="question-button {current.position === p.position
												? 'current'
												: ''} {p.submitted ? 'submitted' : ''}"
											disabled={busy}
											aria-label={`질문 ${p.position + 1} · ${p.submitted ? '제출 완료' : p.draft ? '초안 저장됨' : '미검수'}`}
											aria-current={current.position === p.position ? 'step' : undefined}
											title={p.submitted ? '제출 완료' : p.draft ? '초안 저장됨' : '미검수'}
											on:click={() => perform(() => loadCase(p.position))}
											>{p.position + 1}{p.submitted ? ' ✓' : p.draft ? ' ·' : ''}</button
										>{/each}
								</div>
							</section>
							{#if selected.completed === selected.total}<div
									class="rounded-xl border border-[#c9d7c3] bg-[#eff4eb] p-5"
								>
									<h3 class="font-semibold text-[#3f5840]">{number}회차 검수를 마쳤습니다.</h3>
									<p class="mt-2 text-sm leading-7 text-gray-600">
										{selected.completed > selected.synced
											? '동기화 대기 중인 판정이 있습니다. 위의 MLflow 동기화를 눌러 채점 run에 반영하세요.'
											: '이 회차의 판정이 모두 채점 run에 동기화되어 있습니다.'} 같은 답변을 다시 검수하려면
										새 회차를 시작하세요.
									</p>
								</div>{/if}
							<div class="review-workspace">
								<section class="panel answer-panel" bind:this={answerPanel}>
									<div class="mb-4 flex flex-wrap items-center justify-between gap-3">
										<h2
											bind:this={questionHeading}
											tabindex="-1"
											class="font-semibold outline-none"
										>
											질문 {current.position + 1} / {current.total}
										</h2>
										<span class="status-pill"
											>{current.saved ? '제출 완료 · 읽기 전용' : '검수 중'}</span
										>
									</div>
									{#if current.dimensions[0].inputs.history?.length}<details class="mb-4 text-sm">
											<summary class="cursor-pointer text-gray-500">이전 대화 보기</summary
											>{#each current.dimensions[0].inputs.history as m}<p
													class="mt-3 whitespace-pre-wrap leading-7"
												>
													<strong>{m.role === 'user' ? '사용자' : '답변'}</strong> · {m.content}
												</p>{/each}
										</details>{/if}
									<p class="whitespace-pre-wrap text-lg font-medium leading-8">
										{current.dimensions[0].inputs.question}
									</p>
									<div class="mt-5 border-t border-gray-100 pt-5">
										<ResponseMessage message={turn} />
									</div>
								</section>
								<section class="panel min-w-0">
									<div class="mb-4 flex items-center justify-between gap-3">
										<h2 class="font-semibold">판정 항목</h2>
										<span class="text-xs text-gray-400"
											>{completeCount}/{current.dimensions.length}개 작성</span
										>
									</div>
									<div class="flex flex-wrap gap-2" aria-label="평가 항목">
										{#each current.dimensions as d}<button
												class="dimension-button {active === d.dimension ? 'selected' : ''}"
												aria-pressed={active === d.dimension}
												on:click={() => (active = d.dimension)}
												>{names[d.dimension]}<span class="text-[11px]"
													>{filled(d.dimension, scores)
														? scores[d.dimension].status === 'not_applicable'
															? '해당 없음'
															: `${scores[d.dimension].score}점`
														: '미작성'}</span
												></button
											>{/each}
									</div>
									{#if entry}<div class="mt-5">
											<h3 class="font-semibold">{names[active]}</h3>
											<p class="mt-2 text-sm leading-7 text-gray-600">{entry.rubric.description}</p>
											<div class="mt-3 space-y-2 rounded-lg bg-gray-50 p-4 text-sm">
												{#each entry.scale as value}
													<p>
														<strong>{value} · {entry.scale_labels[String(value)]}</strong> — {entry
															.rubric.levels[String(value)]}
													</p>
												{/each}
												<p class="text-xs text-gray-500">N/A: {entry.rubric.not_applicable}</p>
											</div>
											{#if active === 'groundedness'}<div class="mt-5">
													<Evidence
														sources={entry.outputs.generation_sources ?? []}
														citations={entry.outputs.citations ?? []}
													/>
												</div>{/if}
											{#if entry.expectations}<div class="mt-5 space-y-3">
													{#each Object.entries(entry.expectations) as [key, value]}{#if value !== null && value !== undefined}<details
																class="rounded-xl border border-gray-200 p-4"
																open
															>
																<summary class="cursor-pointer text-sm font-medium"
																	>{refNames[key] ?? key}</summary
																>
																<div class="mt-3"><Reference {value} /></div>
															</details>{/if}{/each}
												</div>{/if}
											{#if active === 'behavior' && entry.outputs.agent_steps?.length}<details
													class="mt-4 text-xs"
												>
													<summary class="cursor-pointer">도구 실행 내역 보기</summary>
													<pre class="mt-3 whitespace-pre-wrap break-words">{JSON.stringify(
															entry.outputs.agent_steps,
															null,
															2
														)}</pre>
												</details>{/if}
											<div class="mt-6 border-t border-gray-100 pt-5">
												<fieldset disabled={busy || !!current.saved || entry.forced_na}>
													<legend class="mb-3 text-sm font-medium">판정</legend>
													<div class="flex flex-wrap gap-2">
														{#each entry.scale as value}
															<label
																class="score-option {scores[active].status === 'scored' &&
																scores[active].score === value
																	? 'chosen'
																	: ''}"
															>
																<input
																	type="radio"
																	name={active}
																	checked={scores[active].status === 'scored' &&
																		scores[active].score === value}
																	on:change={() => {
																		scores[active].score = value;
																		scores[active].status = 'scored';
																	}}
																/>
																{value} · {entry.scale_labels[String(value)]}
															</label>
														{/each}
														{#if !['behavior', 'relevance'].includes(active)}
															<label
																class="score-option {scores[active].status === 'not_applicable'
																	? 'chosen'
																	: ''}"
															>
																<input
																	type="radio"
																	name={active}
																	checked={scores[active].status === 'not_applicable'}
																	on:change={() => {
																		scores[active].score = null;
																		scores[active].status = 'not_applicable';
																	}}
																/>해당 없음
															</label>
														{/if}
													</div>
												</fieldset>
												{#if entry.forced_na}<p class="mt-3 text-xs leading-6 text-gray-500">
														이 문항·응답에는 적용하지 않는 항목으로, 해당 없음으로 고정됩니다.
													</p>{/if}<label class="mt-4 block text-sm font-medium"
													>판정 근거<textarea
														class="input mt-2 block min-h-28 w-full font-normal leading-7"
														maxlength="4000"
														placeholder="어떤 답변이나 근거가 이 판정을 뒷받침하나요?"
														disabled={busy || !!current.saved}
														bind:value={scores[active].rationale}></textarea></label
												>
											</div>
										</div>{/if}
								</section>
							</div>
							<div class="review-footer">
								<div class="text-xs" aria-live="polite">
									{#if current.saved}<span class="text-[#3f5840]"
											>제출한 판정은 보존됩니다. 다시 평가하려면 새 회차를 시작하세요.</span
										>{:else if draftState === 'error'}<span class="text-red-700">{draftError}</span
										><button
											class="ml-2 underline"
											disabled={busy}
											on:click={() => {
												draftBlocked = false;
												void perform(flushDraft);
											}}>초안 저장 재시도</button
										><button
											class="ml-2 underline"
											disabled={busy}
											on:click={() => {
												if (confirm('이 탭의 미저장 입력을 버리고 서버 초안을 불러올까요?')) {
													draftBlocked = false;
													draftSnapshot = JSON.stringify(scores);
													void perform(() => loadCase(current!.position));
												}
											}}>서버 초안 불러오기</button
										>{:else}<span class="text-gray-500"
											>{draftState === 'saving'
												? '초안 저장 중…'
												: dirty
													? '초안 저장 대기…'
													: draftState === 'saved'
														? '초안이 자동 저장되었습니다. 아직 최종 제출 전입니다.'
														: '입력한 판정과 의견은 자동으로 초안 저장됩니다.'}</span
										>{/if}
								</div>
								<div class="flex flex-wrap items-center justify-between gap-3">
									<button
										class="button"
										disabled={busy || current.position === 0}
										on:click={() => perform(() => loadCase(current!.position - 1))}
										>← 이전 질문</button
									>
									<div class="flex flex-wrap items-center gap-3">
										{#if !current.saved && !complete}<span class="text-xs text-gray-500"
												>판정·근거 {current.dimensions.length - completeCount}개 항목 남음</span
											><button
												class="button"
												disabled={busy}
												on:click={() =>
													(active = current!.dimensions.find(
														(e) => !filled(e.dimension, scores)
													)!.dimension)}>미작성 항목으로</button
											>{/if}{#if current.saved}<button
												class="button"
												disabled={busy || current.position + 1 >= current.total}
												on:click={() => perform(() => loadCase(current!.position + 1))}
												>다음 질문 →</button
											>{:else}<button
												class="button primary"
												disabled={busy || !complete}
												on:click={() => perform(saveNext)}
												>{busy
													? '처리 중…'
													: selected.completed + 1 === selected.total
														? '최종 질문 제출 · 회차 완료'
														: '제출하고 다음 질문 →'}</button
											>{/if}
									</div>
								</div>
							</div>
						{:else}<section class="panel py-16 text-center">
								<p class="text-lg font-medium">
									{own(run, rounds).length
										? '새로운 판단을 기록해 보세요.'
										: '내 계정으로 첫 검수를 시작하세요.'}
								</p>
								<p class="mx-auto mt-3 max-w-lg text-sm leading-7 text-gray-500">
									답변을 다시 생성하지 않습니다. 같은 답변을 여러 번 검수하면 본인 판정의 변동성을
									비교할 수 있습니다. 이전 회차의 점수·의견은 새 회차에 채워 넣지 않습니다.
								</p>
							</section>{/if}
					{:else}<section class="panel py-24 text-center">
							<p class="text-lg font-medium">검수할 평가 실행을 선택하세요.</p>
							<p class="mt-3 text-sm text-gray-500">
								왼쪽에서 데이터셋과 내 검수 횟수를 확인할 수 있습니다.
							</p>
						</section>{/if}
				</div>
			</div>{/if}
	</main>
</div>

<style>
	.panel {
		border: 1px solid #e3e7df;
		border-radius: 1rem;
		background: white;
		padding: 1.35rem;
	}
	.button {
		border: 1px solid #d9e2d4;
		border-radius: 0.6rem;
		padding: 0.65rem 0.95rem;
		font-size: 0.8125rem;
		background: white;
	}
	.button:hover:enabled {
		background: #f2f6ee;
	}
	button:disabled {
		opacity: 0.4;
	}
	.primary {
		background: #3f5840;
		color: white;
		border-color: #3f5840;
	}
	.primary:hover:enabled {
		background: #304731;
	}
	.input {
		border: 1px solid #d1d5db;
		border-radius: 0.6rem;
		padding: 0.7rem;
		background: white;
	}
	.round-button,
	.dimension-button {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		border: 1px solid #e3e7df;
		border-radius: 0.7rem;
		padding: 0.65rem 0.8rem;
		font-size: 0.8125rem;
	}
	.round-button {
		flex-direction: column;
		align-items: flex-start;
	}
	.selected {
		border-color: #81977b;
		background: #eff4eb;
		color: #3f5840;
	}
	.question-button {
		border: 1px solid #e3e7df;
		border-radius: 0.5rem;
		min-width: 2.4rem;
		padding: 0.4rem 0.5rem;
		font-size: 0.75rem;
	}
	.submitted {
		background: #eff4eb;
		color: #3f5840;
	}
	.current {
		outline: 2px solid #6c8664;
		outline-offset: 2px;
	}
	.status-pill {
		border-radius: 99px;
		background: #f1f3ee;
		color: #52634c;
		padding: 0.35rem 0.65rem;
		font-size: 0.7rem;
	}
	.score-option {
		display: flex;
		align-items: center;
		gap: 0.5rem;
		cursor: pointer;
		border: 1px solid #dce2d7;
		border-radius: 0.65rem;
		padding: 0.8rem 1rem;
		font-size: 0.8125rem;
	}
	.chosen {
		border-color: #81977b;
		background: #eff4eb;
	}
	.review-workspace {
		display: grid;
		gap: 1.25rem;
		align-items: start;
	}
	.review-footer {
		position: sticky;
		bottom: 0;
		display: grid;
		gap: 0.85rem;
		border: 1px solid #e3e7df;
		border-radius: 1rem;
		background: rgba(255, 255, 255, 0.97);
		padding: 1.1rem;
		box-shadow: 0 -4px 20px #20212408;
		z-index: 10;
	}
	@media (min-width: 1280px) {
		.review-workspace {
			grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
		}
		.answer-panel {
			position: sticky;
			top: 1.25rem;
			max-height: calc(100vh - 11rem);
			overflow-y: auto;
		}
	}
</style>
