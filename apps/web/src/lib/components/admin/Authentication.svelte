<script lang="ts">
	import { onMount } from 'svelte';
	import { api, ApiError, errorText, setCsrf, type Session } from '#lib/api.js';
	import ServiceNav from '#lib/components/common/ServiceNav.svelte';
	type Code = {
		id: string;
		label: string;
		kind: 'admin' | 'guest';
		status: string;
		expires_at: string | null;
		created_at: string;
		last_login_at: string | null;
		last_login_ip: string | null;
		requests_per_hour: number;
		requests_last_hour: number;
	};
	let ready = false,
		allowed = false,
		busy = false,
		error = '',
		notice = '',
		label = '',
		days = 30,
		limit = 30,
		filter = 'all',
		page = 1,
		total = 0;
	let codes: Code[] = [],
		issued: { code: string; label: string } | null = null;
	let extendTarget: Code | null = null,
		extension = 30;
	const filters = [
		['all', '전체'],
		['active', '사용 가능'],
		['expiring', '만료 임박'],
		['expired', '만료'],
		['revoked', '폐기']
	];
	const names: Record<string, string> = {
		active: '사용 가능',
		expiring: '만료 임박',
		expired: '만료',
		revoked: '폐기'
	};
	function date(value: string | null) {
		return value
			? new Intl.DateTimeFormat('ko-KR', { dateStyle: 'medium', timeStyle: 'short' }).format(
					new Date(value)
				)
			: '—';
	}
	function showModal(node: HTMLDialogElement) {
		node.showModal();
	}
	async function fail(e: unknown) {
		if (e instanceof ApiError && e.status === 401) {
			location.replace('/');
			return;
		}
		if (e instanceof ApiError && e.status === 403) allowed = false;
		error = errorText(e);
	}
	async function load() {
		const data = await api<{ items: Code[]; total: number }>(
			`/admin/access-codes?status=${filter}&page=${page}`
		);
		codes = data.items;
		total = data.total;
		if (page > Math.max(1, Math.ceil(total / 30))) {
			page = Math.max(1, Math.ceil(total / 30));
			await load();
		}
	}
	async function refresh(reset = false) {
		if (busy) return;
		busy = true;
		error = '';
		try {
			if (reset) page = 1;
			await load();
		} catch (e) {
			await fail(e);
		} finally {
			busy = false;
		}
	}
	async function issue() {
		if (busy) return;
		busy = true;
		error = '';
		notice = '';
		try {
			const result = await api<Code & { code: string }>('/admin/access-codes', 'POST', {
				label: label.trim(),
				expires_days: days,
				requests_per_hour: limit
			});
			issued = { code: result.code, label: result.label };
			label = '';
			filter = 'all';
			page = 1;
			await load();
		} catch (e) {
			await fail(e);
		} finally {
			busy = false;
		}
	}
	async function copy() {
		if (!issued) return;
		try {
			await navigator.clipboard.writeText(issued.code);
			notice = '로그인 코드를 복사했습니다.';
		} catch {
			notice = '코드 영역을 선택해 직접 복사해 주세요.';
		}
	}
	async function revoke(code: Code) {
		if (
			busy ||
			!confirm(
				`“${code.label}” 코드를 폐기할까요? 연결된 모든 기기에서 즉시 로그아웃되며 되돌릴 수 없습니다.`
			)
		)
			return;
		busy = true;
		error = '';
		notice = '';
		try {
			await api(`/admin/access-codes/${code.id}`, 'DELETE');
			notice = '코드를 폐기했습니다.';
			await load();
		} catch (e) {
			await fail(e);
		} finally {
			busy = false;
		}
	}
	async function extend() {
		if (busy || !extendTarget) return;
		busy = true;
		error = '';
		notice = '';
		try {
			await api(`/admin/access-codes/${extendTarget.id}/extend`, 'POST', { days: extension });
			extendTarget = null;
			notice = '코드 유효기간을 연장했습니다. 기존 코드는 그대로 사용할 수 있습니다.';
			await load();
		} catch (e) {
			await fail(e);
		} finally {
			busy = false;
		}
	}
	onMount(() => {
		void (async () => {
			try {
				const session = await api<Session>('/auth/session');
				setCsrf(session.csrf_token);
				allowed = session.identity.role === 'admin';
				if (allowed) await load();
			} catch (e) {
				await fail(e);
			} finally {
				ready = true;
			}
		})();
	});
</script>

<svelte:head><title>로그인 코드 관리 · 엘리스 테크블로그 · Open WebUI</title></svelte:head>
<div class="min-h-screen bg-[#fafbf9] text-gray-900">
	<header class="border-b border-gray-200 bg-white">
		<div class="max-w-6xl mx-auto px-5 py-4 flex flex-wrap justify-between items-center gap-3">
			<a href="/" class="font-semibold flex items-center gap-2"
				><img src="/open-webui-logo.png" alt="" class="size-7" />Open WebUI
				<span class="font-normal text-gray-400 text-sm ml-2">관리자</span></a
			>
			<ServiceNav active="admin" admin={allowed} />
		</div>
	</header>
	<main class="max-w-6xl mx-auto px-5 py-9">
		{#if !ready}<p class="text-gray-500" role="status">접근 권한을 확인하고 있어요…</p>
		{:else if !allowed}<h1 class="text-2xl font-semibold">관리자 전용 화면입니다.</h1>
			<p class="text-gray-500 mt-3">관리자 코드로 로그인한 뒤 다시 접속해 주세요.</p>
		{:else}
			<div class="mb-8">
				<p class="text-xs text-[#6c7c6b] tracking-widest mb-2">ADMIN · AUTHENTICATION</p>
				<h1 class="text-3xl font-semibold tracking-tight">로그인 코드 관리</h1>
				<p class="text-sm text-gray-500 mt-3 leading-7">
					방문자마다 별도의 코드를 발급하세요. 대화는 코드별로 보관되며, 같은 코드를 사용하는
					기기에서는 같은 대화가 표시됩니다.
				</p>
			</div>
			{#if error}<p role="alert" class="rounded-xl bg-red-50 text-red-700 p-4 text-sm mb-5">
					{error}
				</p>{/if}
			{#if notice}<p role="status" class="rounded-xl bg-[#edf3e8] text-[#3f5840] p-4 text-sm mb-5">
					{notice}
				</p>{/if}
			<section
				class="rounded-2xl border border-gray-200 bg-white p-5 mb-8"
				aria-labelledby="create-code"
			>
				<h2 id="create-code" class="text-base font-semibold">새 방문자 코드 발급</h2>
				<form
					on:submit|preventDefault={issue}
					class="grid sm:grid-cols-2 lg:grid-cols-[2fr_1fr_1fr_auto] gap-4 items-end mt-4"
				>
					<label class="text-xs text-gray-500"
						>이름 / 용도<input
							bind:value={label}
							required
							maxlength="80"
							placeholder="예: 프로젝트 리뷰 · 홍길동"
							class="block mt-2 border border-gray-200 rounded-lg w-full p-2.5 text-sm text-gray-900"
						/></label
					>
					<label class="text-xs text-gray-500"
						>유효기간 (일)<input
							type="number"
							bind:value={days}
							required
							min="1"
							max="3660"
							class="block mt-2 border border-gray-200 rounded-lg w-full p-2.5 text-sm text-gray-900"
						/></label
					>
					<label class="text-xs text-gray-500"
						>시간당 질문 한도<input
							type="number"
							bind:value={limit}
							required
							min="1"
							max="1000"
							class="block mt-2 border border-gray-200 rounded-lg w-full p-2.5 text-sm text-gray-900"
						/></label
					>
					<button
						disabled={busy || !label.trim()}
						class="rounded-lg bg-[#263529] text-white px-5 py-2.5 text-sm disabled:opacity-50"
						>코드 발급</button
					>
				</form>
			</section>
			<section aria-label="발급한 로그인 코드">
				<div class="flex flex-wrap justify-between gap-4 mb-4 items-center">
					<div class="flex flex-wrap gap-1" aria-label="코드 상태 필터">
						{#each filters as [value, text]}<button
								disabled={busy}
								aria-pressed={filter === value}
								class="px-3 py-2 rounded-lg text-sm {filter === value
									? 'bg-[#e7eee1] text-[#3f5840] font-medium'
									: 'text-gray-500 hover:bg-gray-100'}"
								on:click={() => {
									filter = value;
									void refresh(true);
								}}>{text}</button
							>{/each}
					</div>
					<button
						disabled={busy}
						class="text-sm text-gray-500 hover:underline"
						on:click={() => refresh()}>새로고침 ↻</button
					>
				</div>
				<div class="overflow-x-auto rounded-2xl border border-gray-200 bg-white">
					<table class="w-full text-left text-sm min-w-[840px]">
						<thead class="bg-gray-50 border-b border-gray-200 text-xs text-gray-500"
							><tr
								><th class="px-4 py-3 font-medium">코드 이름</th><th class="px-4 py-3 font-medium"
									>상태</th
								><th class="px-4 py-3 font-medium">만료일</th><th class="px-4 py-3 font-medium"
									>최근 로그인</th
								><th class="px-4 py-3 font-medium">최근 1시간 질문</th><th
									class="px-4 py-3 font-medium">관리</th
								></tr
							></thead
						><tbody>
							{#each codes as code (code.id)}<tr class="border-b last:border-b-0 border-gray-100"
									><td class="px-4 py-4 font-medium max-w-56 break-words"
										>{code.label}{#if code.kind === 'admin'}<span
												class="block mt-1 text-[11px] text-gray-400">관리자</span
											>{/if}</td
									><td class="px-4 py-4"
										><span
											class="text-xs rounded-full px-2.5 py-1 {code.status === 'active'
												? 'bg-green-50 text-green-700'
												: code.status === 'expiring'
													? 'bg-amber-50 text-amber-700'
													: code.status === 'revoked'
														? 'bg-red-50 text-red-700'
														: 'bg-gray-100 text-gray-500'}">{names[code.status]}</span
										></td
									><td class="px-4 py-4 text-xs text-gray-500">{date(code.expires_at)}</td><td
										class="px-4 py-4 text-xs text-gray-500"
										>{date(code.last_login_at)}{#if code.last_login_ip}<span
												class="block mt-1 text-[11px] text-gray-400">{code.last_login_ip}</span
											>{/if}</td
									><td class="px-4 py-4 text-gray-600"
										>{code.requests_last_hour} / {code.kind === 'admin'
											? '제한 없음'
											: code.requests_per_hour}</td
									><td class="px-4 py-4"
										>{#if code.kind === 'guest' && code.status !== 'revoked'}<div
												class="flex gap-3"
											>
												<button
													disabled={busy}
													on:click={() => {
														extendTarget = code;
														extension = 30;
													}}
													class="text-[#466047] hover:underline">연장</button
												><button
													disabled={busy}
													on:click={() => revoke(code)}
													class="text-red-600 hover:underline">폐기</button
												>
											</div>{:else}<span class="text-xs text-gray-400"
												>{code.kind === 'admin' ? '서버에서 관리' : '폐기됨'}</span
											>{/if}</td
									></tr
								>{/each}
							{#if !codes.length}<tr
									><td colspan="6" class="p-10 text-center text-gray-400"
										>해당하는 코드가 없습니다.</td
									></tr
								>{/if}
						</tbody>
					</table>
				</div>
				<div class="flex justify-between items-center text-xs text-gray-500 mt-4">
					<span>총 {total}개 · {page} / {Math.max(1, Math.ceil(total / 30))}페이지</span>
					<div class="flex gap-4">
						<button
							disabled={busy || page <= 1}
							class="disabled:opacity-30"
							on:click={() => {
								page--;
								void refresh();
							}}>← 이전</button
						><button
							disabled={busy || page * 30 >= total}
							class="disabled:opacity-30"
							on:click={() => {
								page++;
								void refresh();
							}}>다음 →</button
						>
					</div>
				</div>
			</section>
		{/if}
	</main>
</div>
{#if issued}
	<dialog
		use:showModal
		on:cancel={() => {
			issued = null;
		}}
		aria-labelledby="issued-title"
		class="m-auto w-[calc(100%-2rem)] max-w-lg rounded-2xl p-6 backdrop:bg-black/30 shadow-xl"
	>
		<h2 id="issued-title" class="text-xl font-semibold">로그인 코드가 발급되었습니다.</h2>
		<p class="mt-3 text-sm text-gray-500 leading-7">
			{issued.label}님에게 전달하세요. 코드는 이 화면에서만 확인할 수 있습니다. 창을 닫기 전에
			복사해 두세요.
		</p>
		<label class="block mt-5 text-xs text-gray-500"
			>발급된 코드<input
				readonly
				value={issued.code}
				class="block w-full border rounded-xl p-3 mt-2 font-mono text-sm text-gray-900"
				on:focus={(e) => e.currentTarget.select()}
			/></label
		>{#if notice}<p class="text-xs text-[#466047] mt-2" role="status">{notice}</p>{/if}
		<div class="flex justify-end gap-3 mt-6">
			<button class="px-4 py-2 text-sm text-gray-500" on:click={() => (issued = null)}>닫기</button
			><button class="px-4 py-2 bg-[#263529] text-white rounded-lg text-sm" on:click={copy}
				>코드 복사</button
			>
		</div>
	</dialog>
{/if}
{#if extendTarget}
	<dialog
		use:showModal
		on:cancel={(event) => {
			if (busy) event.preventDefault();
			else extendTarget = null;
		}}
		aria-labelledby="extend-title"
		class="m-auto w-[calc(100%-2rem)] max-w-md rounded-2xl p-6 backdrop:bg-black/30 shadow-xl"
	>
		<h2 id="extend-title" class="text-xl font-semibold">코드 유효기간 연장</h2>
		<p class="text-sm text-gray-500 leading-7 mt-3">
			{extendTarget.label} · 현재 만료일 {date(extendTarget.expires_at)}<br />유효한 코드는 남은
			기간에, 만료된 코드는 지금부터 기간을 추가합니다.
		</p>
		<form on:submit|preventDefault={extend}>
			<label class="block mt-4 text-sm"
				>추가할 기간 (일)<input
					type="number"
					required
					min="1"
					max="3660"
					bind:value={extension}
					class="block border rounded-lg p-2 mt-2 w-full"
				/></label
			>{#if error}<p class="text-sm text-red-700 mt-3" role="alert">{error}</p>{/if}
			<div class="flex justify-end gap-3 mt-5">
				<button
					type="button"
					disabled={busy}
					class="text-sm px-4 py-2 text-gray-500"
					on:click={() => (extendTarget = null)}>취소</button
				><button disabled={busy} class="text-sm px-4 py-2 rounded-lg bg-[#263529] text-white"
					>기간 연장</button
				>
			</div>
		</form>
	</dialog>
{/if}
