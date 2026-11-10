<script lang="ts">
	export let active: 'chat' | 'review' | 'admin';
	export let admin = false;
	export let disabled = false;
	export let navigate: ((href: string) => void) | undefined = undefined;
	const links = [
		{ id: 'chat', href: '/', label: '대화' },
		{ id: 'review', href: '/review', label: '사람 검수' },
		{ id: 'admin', href: '/admin/authentication', label: '관리자' }
	];
</script>

<nav aria-label="서비스 이동" class="ml-auto flex flex-wrap items-center justify-end gap-2">
	{#each links.filter((link) => link.id !== 'admin' || admin) as link}
		<a
			href={link.href}
			aria-current={active === link.id ? 'page' : undefined}
			aria-disabled={disabled || active === link.id}
			tabindex={disabled ? -1 : undefined}
			class="whitespace-nowrap rounded-lg border px-3 py-2 text-sm font-medium transition {active ===
			link.id
				? 'border-[#d9e2d4] bg-[#eff4eb] text-[#3f5840]'
				: 'border-gray-200 bg-white text-gray-600 hover:bg-gray-50'} {disabled ? 'opacity-40' : ''}"
			on:click={(event) => {
				if (disabled || active === link.id) {
					event.preventDefault();
					return;
				}
				if (navigate && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
					event.preventDefault();
					navigate(link.href);
				}
			}}>{link.label}</a
		>
	{/each}
</nav>
