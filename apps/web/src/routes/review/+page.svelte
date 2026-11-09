<script lang="ts">
	import { onMount } from 'svelte';
	import { api, ApiError, errorText, setCsrf, type Session, type Turn } from '#lib/api.js';
	import ResponseMessage from '#lib/components/chat/Messages/ResponseMessage.svelte';

	type Label = '' | 'pass' | 'fail' | 'not_applicable';
	type Score = { label: Label; rationale: string };
	type Entry = {
		dimension: string;
		rubric: string;
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
			labels: { dimension: string; label: Label; rationale: string }[];
		} | null;
	};
	type Source = {
		next_position: number;
		run_id: string;
		dataset_id: string;
		kind: string;
		total: number;
		completed: number;
	};
	type Candidate = {
		run_id: string;
		run_name: string;
		status: string;
		start_time: number;
		dataset_id: string;
		dataset_name: string;
		url: string | null;
	};
	let origin: Candidate | null = null,
		originError = '';
	let session: Session | null = null;
	let ready = false,
		busy = false,
		error = '',
		notice = '';
	let sources: Source[] = [],
		candidates: Candidate[] = [];
	let nextPage: string | null = null;
	let run = '',
		current: ReviewCase | null = null,
		active = '';
	let scores: Record<string, Score> = {};
	let reviewers: { id: string; label: string }[] = [];
	let reviewer = '';
	const names: Record<string, string> = {
		correctness: '정확성',
		completeness: '완전성',
		groundedness: '근거 충실도',
		relevance: '응답 적절성',
		behavior: '행동 준수'
	};
	const descriptions: Record<string, string> = {
		correctness:
			'검증된 정답·근거와 모순되거나 핵심 사실을 지어내지 않았는지 평가하세요. 누락은 완전성에서 평가합니다.',
		completeness:
			'필수 항목을 빠짐없이 정확하게 전달했는지 평가하세요. 선택적인 세부 사항은 요구하지 않습니다.',
		groundedness:
			'아래 최종 생성 컨텍스트만 근거로 사실 주장이 뒷받침되는지 평가하세요. 모범 답안이나 외부 지식은 사용하지 않습니다.',
		relevance:
			'실제 질문에 적절히 답했는지 평가하세요. 적절한 추가 질문·근거 부족 안내·거절도 포함됩니다.',
		behavior:
			'문항의 기대 행동과 필수 항목·실패 조건을 기준으로 평가하세요. 무조건적인 거절을 정답으로 취급하지 않습니다.'
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
	function safeUrl(value: unknown) {
		if (typeof value !== 'string') return undefined;
		try {
			const url = new URL(value);
			return ['http:', 'https:'].includes(url.protocol) ? url.href : undefined;
		} catch {
			return undefined;
		}
	}
	async function perform(work: () => Promise<void>) {
		if (busy) return;
		busy = true;
		error = '';
		notice = '';
		try {
			await work();
		} catch (e) {
			if (e instanceof ApiError && e.status === 401) location.assign('/');
			error = errorText(e);
		} finally {
			busy = false;
		}
	}
	async function refresh() {
		sources = (await api<{ items: Source[] }>('/reviews')).items;
	}
	async function candidatesPage(token: string | null = null) {
		const data = await api<{ items: Candidate[]; next_page_token: string | null }>(
			'/admin/review-candidates' + (token ? `?page_token=${encodeURIComponent(token)}` : '')
		);
		candidates = token ? [...candidates, ...data.items] : data.items;
		nextPage = data.next_page_token;
	}
	async function loadCase(position: number) {
		const data = await api<ReviewCase>(`/reviews/${run}/cases/${position}`);
		current = data;
		active = data.dimensions[0].dimension;
		scores = Object.fromEntries(
			data.dimensions.map((e) => {
				const saved = data.saved?.labels.find((s) => s.dimension === e.dimension);
				return [
					e.dimension,
					saved
						? { label: saved.label, rationale: saved.rationale }
						: {
								label: e.forced_na ? 'not_applicable' : '',
								rationale: e.forced_na ? '문항/응답에 적용되지 않는 평가 차원' : ''
							}
				];
			})
		);
	}
	async function open(source: Source) {
		run = source.run_id;
		current = null;
		origin = null;
		originError = '';
		if (session?.identity.role === 'admin') {
			try {
				origin = await api<Candidate>(`/admin/reviews/${run}/origin`);
			} catch {
				originError = '원본 run 정보를 불러오지 못했습니다. 저장된 답변은 계속 검수할 수 있습니다.';
			}
		}
		if (!source.total) {
			notice = '검수 가능한 답변이 없습니다. 생성 실패 문항은 채점에서 제외합니다.';
			return;
		}
		await loadCase(source.next_position);
	}
	async function importRun(candidate: Candidate) {
		await api('/admin/reviews', 'POST', { run_id: candidate.run_id });
		await refresh();
		await open(sources.find((s) => s.run_id === candidate.run_id)!);
	}
	async function saveNext() {
		if (!current) return;
		const position = current.position;
		await api(`/reviews/${run}/cases/${position}`, 'POST', {
			answer_digest: current.answer_digest,
			scores
		});
		await refresh();
		if (position + 1 < current.total) await loadCase(position + 1);
		else {
			await loadCase(position);
			notice = '마지막 질문까지 저장했습니다. MLflow 동기화를 눌러 원본 평가 run에 반영하세요.';
		}
	}
	async function download() {
		const data = await api<{ labels: unknown[] }>(`/reviews/${run}/labels`);
		const url = URL.createObjectURL(
			new Blob([data.labels.map((x) => JSON.stringify(x)).join('\n') + '\n'], {
				type: 'application/x-ndjson'
			})
		);
		const link = document.createElement('a');
		link.href = url;
		link.download = `human-labels-${run}.jsonl`;
		link.click();
		URL.revokeObjectURL(url);
	}
	$: entry = current?.dimensions.find((e) => e.dimension === active);
	$: complete = current?.dimensions.every(
		(e) => scores[e.dimension]?.label && scores[e.dimension]?.rationale.trim()
	);
	$: turn = current
		? ({
				id: current.record_id,
				question: '',
				status: 'completed',
				stage: '',
				error: null,
				result: current.dimensions[0].outputs
			} satisfies Turn)
		: null;
	onMount(() => {
		void perform(async () => {
			session = await api<Session>('/auth/session');
			setCsrf(session.csrf_token);
			await refresh();
			if (session.identity.role === 'admin') {
				let page = 1;
				while (true) {
					const data = await api<{
						items: { id: string; label: string; status: string }[];
						total: number;
						page_size: number;
					}>(`/admin/access-codes?page=${page}`);
					reviewers = [
						...reviewers,
						...data.items.filter((p) => ['active', 'expiring'].includes(p.status))
					];
					if (page * data.page_size >= data.total) break;
					page++;
				}
			}
			ready = true;
		});
	});
</script>

<svelte:head><title>Human Review · 엘리스 테크블로그</title></svelte:head>
<div class="min-h-screen bg-[#fafbf9] text-gray-900">
	<header class="border-b border-gray-200 bg-white">
		<div class="mx-auto max-w-7xl px-5 py-4 flex flex-wrap items-center justify-between gap-3">
			<div class="flex items-center gap-3">
				<img src="/open-webui-logo.png" alt="" class="size-7" /><strong>Human Review</strong><span
					class="text-sm text-gray-500">블라인드 답변 검수</span
				>
			</div>
			<div class="flex items-center gap-4 text-sm">
				<span>{session?.identity.label ?? ''}</span><a href="/" class="underline">대화로 돌아가기</a
				>
			</div>
		</div>
	</header>
	<main class="mx-auto max-w-7xl px-5 py-8">
		{#if error}<p
				role="alert"
				class="mb-5 rounded-xl border border-red-200 bg-red-50 p-4 text-red-800"
			>
				{error}
			</p>{/if}
		{#if notice}<p
				role="status"
				class="mb-5 rounded-xl border border-green-200 bg-green-50 p-4 text-green-900"
			>
				{notice}
			</p>{/if}
		{#if !ready}<p role="status">
				{error ? '접근 권한과 연결 상태를 확인해 주세요.' : '검수 목록을 불러오고 있어요…'}
			</p>
		{:else}
			<div class="mb-7 flex flex-wrap items-start justify-between gap-4">
				<div>
					<p class="text-xs tracking-widest text-[#6c7c6b]">EVALUATION · HUMAN REVIEW</p>
					<h1 class="mt-2 text-3xl font-semibold">답변을 읽고, 근거로 평가하세요.</h1>
					<p class="mt-3 text-sm text-gray-500">
						다른 평가자의 점수와 의견은 표시하지 않습니다. 제출한 평가는 고정되며 로그아웃 후에도
						보존됩니다.
					</p>
				</div>
				{#if session?.identity.role === 'admin'}<button
						class="button"
						disabled={busy}
						on:click={() => perform(() => candidatesPage())}>MLflow 평가 실행 불러오기</button
					>{/if}
			</div>
			{#if candidates.length}<section class="panel mb-6">
					<h2 class="font-semibold">MLflow 평가 실행</h2>
					<div class="mt-3 grid gap-2">
						{#each candidates as candidate}<div
								class="flex flex-wrap items-center justify-between gap-3 border-t border-gray-100 py-3"
							>
								<div class="text-sm">
									<p>
										{candidate.dataset_name ?? candidate.dataset_id ?? '데이터셋'} · {candidate.run_name}
										· {new Date(candidate.start_time).toLocaleString('ko-KR')}
									</p>
									<p class="mt-1 font-mono text-xs text-gray-500">
										{candidate.run_id} · {candidate.status}
									</p>
									{#if safeUrl(candidate.url)}<a
											class="mt-2 inline-block underline text-[#3f5840]"
											href={safeUrl(candidate.url)}
											target="_blank"
											rel="noreferrer">MLflow 원본 run 열기 ↗</a
										>{/if}
								</div>
								<button
									class="button"
									disabled={busy ||
										candidate.status === 'RUNNING' ||
										candidate.status === 'SCHEDULED'}
									on:click={() => perform(() => importRun(candidate))}>검수 목록에 추가</button
								>
							</div>{/each}
						{#if nextPage}<button
								class="button"
								disabled={busy}
								on:click={() => perform(() => candidatesPage(nextPage))}>더 불러오기</button
							>{/if}
					</div>
				</section>{/if}
			<div class="grid gap-6 lg:grid-cols-[250px_minmax(0,1fr)]">
				<aside class="space-y-3">
					<h2 class="text-sm font-semibold text-gray-500">내 검수 목록</h2>
					{#if !sources.length}<p class="text-sm leading-7 text-gray-500">
							배정된 평가가 없습니다. 관리자가 평가 실행을 추가하고 로그인 코드에 검수를 배정할 수
							있습니다.
						</p>{/if}
					{#each sources as source}<button
							class="w-full rounded-xl border p-4 text-left {run === source.run_id
								? 'border-[#81977b] bg-[#eff4eb]'
								: 'border-gray-200 bg-white'}"
							disabled={busy}
							on:click={() => perform(() => open(source))}
							><span class="text-xs uppercase text-gray-500"
								>{source.kind === 'gold' ? '🥇 Gold' : '🛡️ Iron'}</span
							><span class="mt-2 block break-all text-sm font-medium">{source.dataset_id}</span
							><span class="mt-1 block font-mono text-xs text-gray-400"
								>{source.run_id.slice(0, 10)}</span
							><span class="mt-3 block text-sm"
								>{source.completed} / {source.total}개 검수 완료</span
							></button
						>{/each}
				</aside>
				<div class="min-w-0 space-y-5">
					{#if session?.identity.role === 'admin' && run}
						{#if origin}<section class="panel flex flex-wrap items-center justify-between gap-4">
								<div>
									<p class="text-sm font-semibold">
										{origin.dataset_name ?? origin.dataset_id} · {origin.run_name}
									</p>
									<p class="mt-1 text-xs text-gray-500">
										{new Date(origin.start_time).toLocaleString('ko-KR')} · {origin.status}
									</p>
									<p class="mt-2 break-all font-mono text-xs text-gray-500">{origin.run_id}</p>
									<p class="mt-2 text-xs text-gray-500">
										동기화한 사람 판정: 원본 run의 Artifacts → human-review
									</p>
								</div>
								{#if safeUrl(origin.url)}<a
										class="button text-[#3f5840]"
										href={safeUrl(origin.url)}
										target="_blank"
										rel="noreferrer">MLflow 원본 run 열기 ↗</a
									>{/if}
							</section>
						{:else if originError}<p role="status" class="text-sm text-gray-500">
								{originError}
							</p>{/if}
					{/if}
					{#if run}<div class="panel flex flex-wrap items-center gap-3">
							<button
								class="button"
								disabled={busy}
								on:click={() =>
									perform(async () => {
										await api(`/reviews/${run}/sync`, 'POST');
										notice = '저장한 평가를 MLflow 원본 run에 동기화했습니다.';
										if (current) await loadCase(current.position);
									})}>MLflow 동기화</button
							><button class="button" disabled={busy} on:click={() => perform(download)}
								>내 평가 JSONL 내려받기</button
							>
							{#if session?.identity.role === 'admin'}<label class="ml-auto text-sm"
									>검수자 <select class="input ml-2" bind:value={reviewer}
										><option value="">코드 선택</option>{#each reviewers as person}<option
												value={person.id}>{person.label}</option
											>{/each}</select
									></label
								><button
									class="button"
									disabled={busy || !reviewer}
									on:click={() =>
										perform(async () => {
											await api(`/admin/reviews/${run}/assignments`, 'POST', {
												reviewer_id: reviewer
											});
											notice = '선택한 코드에 검수를 배정했습니다.';
										})}>배정</button
								>{/if}
						</div>{/if}
					{#if current && turn}
						<section class="panel">
							<div class="mb-4 flex items-center justify-between gap-3">
								<h2 class="font-semibold">질문 {current.position + 1} / {current.total}</h2>
								{#if current.saved}<span
										class="rounded-full bg-green-50 px-3 py-1 text-xs text-green-800"
										>저장됨 · {current.saved.synced ? '동기화 완료' : '동기화 대기'}</span
									>{/if}
							</div>
							{#if current.dimensions[0].inputs.history?.length}<details class="mb-4 text-sm">
									<summary class="cursor-pointer text-gray-500">이전 대화 보기</summary
									>{#each current.dimensions[0].inputs.history as message}<p
											class="mt-2 whitespace-pre-wrap"
										>
											<strong>{message.role === 'user' ? '사용자' : '답변'}</strong> · {message.content}
										</p>{/each}
								</details>{/if}
							<p class="whitespace-pre-wrap text-lg font-medium leading-8">
								{current.dimensions[0].inputs.question}
							</p>
							<div class="mt-5 border-t border-gray-100 pt-5">
								<ResponseMessage message={turn} />
							</div>
						</section>
						<section class="panel">
							<div class="flex flex-wrap gap-2" aria-label="평가 항목">
								{#each current.dimensions as dimension}<button
										class="rounded-lg border px-4 py-2 text-sm {active === dimension.dimension
											? 'border-[#81977b] bg-[#eff4eb]'
											: 'border-gray-200'}"
										on:click={() => (active = dimension.dimension)}
										>{names[dimension.dimension]}
										{scores[dimension.dimension]?.label ? '✓' : ''}</button
									>{/each}
							</div>
							{#if entry}<div class="mt-6">
									<h3 class="text-lg font-semibold">{names[active]}</h3>
									<p class="mt-2 text-sm leading-7 text-gray-600">{descriptions[active]}</p>
									<details class="mt-3 text-sm">
										<summary class="cursor-pointer text-gray-500"
											>Judge와 동일한 평가 기준 원문</summary
										>
										<pre
											class="mt-3 whitespace-pre-wrap rounded-lg bg-gray-50 p-4 text-xs leading-6">{entry.rubric}</pre>
									</details>
									{#if active === 'groundedness'}<h4 class="mt-5 font-medium">
											최종 답변 생성에 제공된 근거
										</h4>
										{#each entry.outputs.generation_sources ?? [] as source, i}<article
												class="mt-3 rounded-lg border border-gray-200 p-4"
											>
												<p class="text-sm font-semibold">
													{String(source.title ?? `근거 ${i + 1}`)}
												</p>
												{#if safeUrl(source.url)}<a
														href={safeUrl(source.url)}
														target="_blank"
														rel="noreferrer"
														class="mt-1 block break-all text-xs text-gray-500 underline"
														>원문 열기</a
													>{/if}
												<p class="mt-3 whitespace-pre-wrap break-words text-sm leading-7">
													{String(source.text ?? '')}
												</p>
											</article>{:else}<p class="mt-3 text-sm text-gray-500">
												제공된 생성 근거가 없습니다.
											</p>{/each}{/if}
									{#if entry.expectations}<div class="mt-5 space-y-3">
											{#each Object.entries(entry.expectations) as [key, value]}{#if value !== null && value !== undefined}<details
														class="rounded-lg border border-gray-200 p-4"
														open
													>
														<summary class="cursor-pointer text-sm font-medium"
															>{refNames[key] ?? key}</summary
														>
														<pre
															class="mt-3 whitespace-pre-wrap break-words text-sm leading-7">{typeof value ===
															'string'
																? value
																: JSON.stringify(value, null, 2)}</pre>
													</details>{/if}{/each}
										</div>{/if}
									{#if active === 'behavior' && entry.outputs.agent_steps?.length}<details
											class="mt-4 text-sm"
										>
											<summary>검색 도구 실행 내역</summary>
											<pre class="mt-3 whitespace-pre-wrap break-words">{JSON.stringify(
													entry.outputs.agent_steps,
													null,
													2
												)}</pre>
										</details>{/if}
									<fieldset class="mt-6" disabled={busy || !!current.saved || entry.forced_na}>
										<legend class="mb-3 text-sm font-medium">판정</legend>
										<div class="flex flex-wrap gap-3">
											{#each [['pass', '충족'], ['fail', '미충족'], ...(active === 'behavior' ? [] : [['not_applicable', '해당 없음']])] as [value, label]}<label
													class="flex cursor-pointer items-center gap-2 rounded-lg border border-gray-200 px-4 py-3 text-sm"
													><input
														type="radio"
														name={active}
														{value}
														bind:group={scores[active].label}
													/>{label}</label
												>{/each}
										</div>
									</fieldset>
									{#if entry.forced_na}<p class="mt-2 text-xs text-gray-500">
											Judge와 동일한 적용 조건에 따라 이 항목은 해당 없음입니다.
										</p>{/if}
									<label class="mt-4 block text-sm font-medium"
										>판정 근거<textarea
											class="input mt-2 block min-h-28 w-full font-normal"
											maxlength="4000"
											placeholder="판정을 결정한 답변·근거·기준을 구체적으로 적어주세요."
											disabled={busy || !!current.saved}
											bind:value={scores[active].rationale}></textarea></label
									>
								</div>{/if}
						</section>
						<div class="flex flex-wrap items-center justify-between gap-3">
							<button
								class="button"
								disabled={busy || current.position === 0}
								on:click={() => perform(() => loadCase(current!.position - 1))}>← 이전 질문</button
							><span class="text-xs text-gray-500">이동하면 제출하지 않은 입력은 사라집니다.</span
							>{#if current.saved}<button
									class="button"
									disabled={busy || current.position + 1 >= current.total}
									on:click={() => perform(() => loadCase(current!.position + 1))}
									>다음 질문 →</button
								>{:else}<button
									class="button primary"
									disabled={busy || !complete}
									on:click={() => perform(saveNext)}
									>{busy ? '저장 중…' : '저장하고 다음 질문 →'}</button
								>{/if}
						</div>
					{:else if !run}<section class="panel py-20 text-center text-gray-500">
							왼쪽에서 검수할 평가 실행을 선택하세요.
						</section>{/if}
				</div>
			</div>
		{/if}
	</main>
</div>

<style>
	.panel {
		border: 1px solid #e5e7eb;
		border-radius: 1rem;
		background: white;
		padding: 1.5rem;
	}
	.button {
		border: 1px solid #d9e2d4;
		border-radius: 0.6rem;
		padding: 0.6rem 0.9rem;
		font-size: 0.875rem;
		background: white;
	}
	.button:disabled {
		opacity: 0.4;
	}
	.primary {
		background: #3f5840;
		color: white;
		border-color: #3f5840;
	}
	.input {
		border: 1px solid #d1d5db;
		border-radius: 0.5rem;
		padding: 0.6rem;
		background: white;
	}
</style>
