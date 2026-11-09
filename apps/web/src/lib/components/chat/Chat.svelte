<!-- Open WebUI Chat.svelte shell, reduced to this service's chat API; see UPSTREAM.md. -->
<script lang="ts">
	import { onMount, tick } from 'svelte';
	import { api, ApiError, errorText, setCsrf, type Conversation, type Item } from '#lib/api.js';
	import Messages from './Messages.svelte';
	import MessageInput from './MessageInput.svelte';
	let ready = false,
		authenticated = false,
		code = '',
		error = '',
		busy = false,
		prompt = '',
		sidebar = false,
		about = false;
	let items: Item[] = [],
		current: Conversation | null = null,
		scroll: HTMLDivElement;
	let pending: { request_id: string; question: string; conversation_id: string } | null = null;
	let timer: ReturnType<typeof setTimeout>;
	let navigation = 0;
	$: generating = current?.turns.some((t) => ['queued', 'running'].includes(t.status)) ?? false;
	async function failure(e: unknown) {
		if (e instanceof ApiError && e.status === 401) {
			authenticated = false;
			setCsrf('');
			error = '세션이 만료되었습니다. 다시 접속해 주세요.';
		} else error = errorText(e);
	}
	async function refreshList() {
		items = (await api<{ items: Item[] }>('/conversations')).items;
	}
	async function open(id: string) {
		clearTimeout(timer);
		error = '';
		sidebar = false;
		const sequence = ++navigation;
		const selected = await api<Conversation>(`/conversations/${id}`);
		if (sequence !== navigation) return;
		current = selected;
		history.replaceState({}, '', `/c/${id}`);
		await refreshList();
		await tick();
		if (scroll) scroll.scrollTop = scroll.scrollHeight;
		poll(id);
	}
	function poll(id: string) {
		clearTimeout(timer);
		if (!current?.turns.some((t) => ['queued', 'running'].includes(t.status))) return;
		timer = setTimeout(async () => {
			try {
				const next = await api<Conversation>(`/conversations/${id}`);
				if (current?.id !== id) return;
				const nearBottom =
					scroll && scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 120;
				current = next;
				await tick();
				if (nearBottom && scroll) scroll.scrollTop = scroll.scrollHeight;
				poll(id);
			} catch (e) {
				await failure(e);
				if (authenticated) timer = setTimeout(() => poll(id), 3000);
			}
		}, 1200);
	}
	async function boot() {
		try {
			const session = await api<{ csrf_token: string }>('/auth/session');
			setCsrf(session.csrf_token);
			authenticated = true;
			await refreshList();
			const match = location.pathname.match(/^\/c\/([a-f0-9-]+)$/);
			if (match) await open(match[1]);
			const saved = sessionStorage.getItem('pending-turn');
			if (saved) {
				pending = JSON.parse(saved);
				if (pending) {
					prompt = pending.question;
					await open(pending.conversation_id);
					error = '전송 결과를 확인하지 못한 질문이 있습니다. 같은 요청으로 다시 확인해 주세요.';
				}
			}
		} catch (e) {
			if (!(e instanceof ApiError && e.status === 401)) await failure(e);
		}
		ready = true;
	}
	async function login() {
		busy = true;
		error = '';
		try {
			const session = await api<{ csrf_token: string }>('/auth/session', 'POST', { code });
			code = '';
			setCsrf(session.csrf_token);
			authenticated = true;
			await refreshList();
		} catch (e) {
			await failure(e);
		} finally {
			busy = false;
		}
	}
	function showDialog(node: HTMLDialogElement) {
		node.showModal();
	}
	function newChat() {
		if (pending) {
			error = '전송 결과를 먼저 확인해 주세요.';
			return;
		}
		navigation++;
		clearTimeout(timer);
		current = null;
		error = '';
		prompt = '';
		sidebar = false;
		history.replaceState({}, '', '/');
	}
	async function send() {
		if (busy || (!pending && (generating || !prompt.trim()))) return;
		busy = true;
		error = '';
		try {
			if (!pending) {
				if (!current) current = await api<Conversation>('/conversations', 'POST');
				pending = {
					request_id: crypto.randomUUID(),
					question: prompt.trim(),
					conversation_id: current.id
				};
				sessionStorage.setItem('pending-turn', JSON.stringify(pending));
			}
			const request = pending;
			await api(`/conversations/${request.conversation_id}/turns`, 'POST', {
				request_id: request.request_id,
				question: request.question
			});
			sessionStorage.removeItem('pending-turn');
			pending = null;
			prompt = '';
			await open(request.conversation_id);
		} catch (e) {
			// Definitive rejections can be corrected; ambiguous network errors retain the same UUID.
			if (e instanceof ApiError && e.status < 500) {
				pending = null;
				sessionStorage.removeItem('pending-turn');
			}
			await failure(e);
		} finally {
			busy = false;
		}
	}
	async function remove() {
		if (!current || !confirm('이 대화 기록을 삭제할까요?')) return;
		try {
			await api(`/conversations/${current.id}`, 'DELETE');
			newChat();
			await refreshList();
		} catch (e) {
			await failure(e);
		}
	}
	async function logout() {
		try {
			await api('/auth/session', 'DELETE');
			clearTimeout(timer);
			authenticated = false;
			current = null;
			items = [];
			pending = null;
			sessionStorage.removeItem('pending-turn');
			setCsrf('');
			history.replaceState({}, '', '/');
		} catch (e) {
			await failure(e);
		}
	}
	onMount(() => {
		void boot();
		return () => clearTimeout(timer);
	});
</script>

<svelte:head
	><title>엘리스 테크블로그 · Open WebUI</title><meta
		name="description"
		content="엘리스 한국어 테크블로그를 출처와 함께 살펴보는 대화"
	/></svelte:head
>
{#if !ready}
	<main class="min-h-screen grid place-items-center text-gray-500" aria-busy="true">준비 중…</main>
{:else if !authenticated}
	<main class="min-h-screen flex items-center justify-center bg-[#fafaf9] p-6">
		<div class="w-full max-w-sm">
			<div class="flex items-center gap-2 mb-12">
				<img src="/open-webui-logo.png" class="size-8" alt="Open WebUI" /><span
					class="font-semibold text-xl">Open WebUI</span
				>
			</div>
			<p class="text-xs tracking-widest text-gray-400 mb-3">ELICE TECHBLOG</p>
			<h1 class="text-3xl font-semibold tracking-tight mb-3">
				기술 이야기,<br />대화로 탐색하세요.
			</h1>
			<p class="text-sm text-gray-500 leading-7 mb-8">
				엘리스 테크블로그를 바탕으로 답하고,<br />확인할 수 있는 출처를 함께 보여드려요.
			</p>
			<form on:submit|preventDefault={login} class="space-y-3">
				<label for="access-code" class="text-sm font-medium">공유 코드</label>
				<input
					id="access-code"
					type="password"
					bind:value={code}
					required
					autocomplete="current-password"
					placeholder="전달받은 코드를 입력하세요"
					class="w-full border border-gray-200 rounded-xl p-3 bg-white outline-gray-400"
				/>
				<button
					class="w-full rounded-xl bg-[#232c25] text-white p-3 disabled:opacity-50"
					disabled={busy}>{busy ? '접속 중…' : '대화 시작하기'}</button
				>
			</form>
			{#if error}<p class="text-sm text-red-700 mt-4" role="alert">{error}</p>{/if}
			<p class="text-xs text-gray-400 mt-6 leading-5">
				이 브라우저의 대화만 표시됩니다. 세션 만료 후 기록은 정리됩니다.
			</p>
			<button class="text-xs text-gray-400 underline mt-4" on:click={() => (about = !about)}
				>서비스 정보 · 라이선스</button
			>
		</div>
	</main>
{:else}
	<div class="flex h-screen max-h-[100dvh]">
		{#if sidebar}<button
				class="fixed inset-0 bg-black/20 z-20 md:hidden"
				aria-label="메뉴 닫기"
				on:click={() => (sidebar = false)}
			></button>{/if}
		<aside
			class="{sidebar
				? 'flex fixed inset-y-0 left-0 z-30'
				: 'hidden'} md:relative md:flex w-64 shrink-0 bg-[#f8f9f7] border-r border-gray-100 flex-col p-4"
		>
			<a
				href="/"
				on:click|preventDefault={newChat}
				class="flex gap-2 items-center font-semibold px-2 py-2"
				><img src="/open-webui-logo.png" class="size-7" alt="" />Open WebUI</a
			>
			<button
				on:click={newChat}
				class="text-left py-3 px-3 rounded-xl hover:bg-gray-200/50 mt-4 font-medium text-sm"
				>＋ 새 대화</button
			>
			<p class="text-[11px] text-gray-400 px-3 mt-7 mb-2">이 브라우저의 대화</p>
			<nav class="flex-1 overflow-y-auto" aria-label="이전 대화">
				{#each items as item}<button
						on:click={() => open(item.id).catch(failure)}
						class="w-full text-left text-sm truncate px-3 py-2.5 rounded-xl {current?.id === item.id
							? 'bg-gray-200/60'
							: 'hover:bg-gray-200/30'}">{item.title}</button
					>{/each}
				{#if !items.length}<p class="text-xs text-gray-400 px-3 py-2">
						대화를 시작하면 여기에 표시됩니다.
					</p>{/if}
			</nav>
			<button class="text-xs text-gray-500 text-left p-2" on:click={() => (about = !about)}
				>서비스 정보 · 라이선스</button
			>
			<button class="text-xs text-gray-500 text-left p-2" on:click={logout}>로그아웃</button>
		</aside>
		<div
			class="h-screen max-h-[100dvh] w-full max-w-full min-w-0 flex flex-col"
			id="chat-container"
			role="presentation"
		>
			<div class="w-full h-full flex flex-col">
				<header
					class="h-16 shrink-0 flex items-center justify-between px-4 md:px-7 border-b border-gray-100"
				>
					<div class="flex items-center gap-3">
						<button
							class="md:hidden p-1"
							aria-label="대화 메뉴 열기"
							on:click={() => (sidebar = !sidebar)}>☰</button
						>
						<div>
							<h1 class="text-sm font-semibold">엘리스 테크블로그</h1>
							<p class="text-[11px] text-gray-400">한국어 기술 문서 기반 대화</p>
						</div>
					</div>
					{#if current}<button
							class="text-xs text-gray-400 hover:text-red-700 disabled:opacity-30"
							disabled={generating || busy}
							on:click={remove}>대화 삭제</button
						>{/if}
				</header>
				<div bind:this={scroll} class="flex-1 overflow-y-auto min-h-0 px-5 md:px-8">
					<div class="max-w-3xl mx-auto">
						{#if current?.turns?.length}<Messages messages={current.turns} />
						{:else}<div class="pt-[15vh] pb-12 max-w-xl mx-auto">
								<p class="text-xs text-[#6c7c6b] tracking-[.2em] mb-4">KNOWLEDGE, CONNECTED</p>
								<h2 class="text-3xl md:text-4xl tracking-tight font-semibold leading-snug">
									읽고 싶은 기술 이야기,<br />어디서부터 찾을까요?
								</h2>
								<p class="text-gray-500 text-sm mt-5 leading-7">
									엘리스 테크블로그의 내용을 찾아 설명해 드릴게요.<br />답변 아래에서 근거가 된
									원문도 확인할 수 있어요.
								</p>
								<div class="grid sm:grid-cols-2 gap-3 mt-8">
									{#each ['엘리스의 AI 인프라는 어떻게 구성되어 있나요?', '블로그에서 다루는 RAG 활용 사례를 알려줘'] as example}<button
											class="border border-gray-200 rounded-2xl text-sm text-left p-4 hover:bg-gray-50 leading-6"
											on:click={() => (prompt = example)}>{example} ↗</button
										>{/each}
								</div>
							</div>{/if}
					</div>
				</div>
				{#if error}<div
						class="max-w-3xl w-full mx-auto px-5 py-2 text-sm text-red-700"
						role="alert"
					>
						{error}{#if pending}<button class="underline ml-2" on:click={send} disabled={busy}
								>같은 요청으로 확인</button
							>{/if}
					</div>{/if}
				<MessageInput bind:prompt disabled={busy || generating || !!pending} onSubmit={send} />
			</div>
		</div>
	</div>
{/if}
{#if about}
	<dialog
		use:showDialog
		on:cancel={() => (about = false)}
		aria-labelledby="about-title"
		class="m-auto backdrop:bg-black/20 rounded-2xl p-7 max-w-md shadow-xl"
	>
		<h2 id="about-title" class="font-semibold mb-3">Open WebUI · 서비스 정보</h2>
		<p class="text-sm text-gray-600 leading-7">
			Open WebUI 프론트엔드 일부를 수정한 독립 서비스입니다. 대화와 검색은 이 프로젝트의 Knowledge
			API에서 처리합니다. Open WebUI Inc.의 공식 서비스가 아닙니다.
		</p>
		<p class="text-xs text-gray-500 mt-4">Copyright (c) 2023– Open WebUI Inc.</p>
		<div class="flex flex-wrap gap-3 text-xs underline mt-3">
			<a href="/licenses/open-webui/LICENSE" target="_blank" rel="noreferrer">LICENSE</a><a
				href="/licenses/open-webui/LICENSE_HISTORY"
				target="_blank"
				rel="noreferrer">LICENSE_HISTORY</a
			><a href="/licenses/open-webui/LICENSE_NOTICE" target="_blank" rel="noreferrer"
				>LICENSE_NOTICE</a
			>
		</div>
		<button
			class="bg-gray-900 text-white rounded-xl px-5 py-2 mt-6 text-sm"
			on:click={() => (about = false)}>닫기</button
		>
	</dialog>
{/if}
