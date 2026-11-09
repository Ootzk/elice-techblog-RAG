export type Citation = {
	source_id: number;
	title: string;
	url: string;
	heading_path: string;
	quote: string;
};
export type Turn = {
	id: string;
	question: string;
	status: string;
	stage: string;
	error: string | null;
	result: null | { status: string; answer: string; reason: string | null; citations: Citation[] };
};
export type Conversation = { id: string; title: string; turns: Turn[] };
export type Item = { id: string; title: string; updated_at: string };
let csrf = '';
export function setCsrf(value: string) {
	csrf = value;
}
export class ApiError extends Error {
	constructor(
		public status: number,
		public code: string
	) {
		super(code);
	}
}
export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
	const response = await fetch(`/api${path}`, {
		method,
		credentials: 'same-origin',
		headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf },
		body: body === undefined ? undefined : JSON.stringify(body)
	});
	const data = response.status === 204 ? null : await response.json();
	if (!response.ok) throw new ApiError(response.status, data.error ?? 'request_failed');
	return data;
}
export function errorText(error: unknown): string {
	if (error instanceof ApiError)
		return (
			(
				{
					invalid_access_code: '공유 코드가 올바르지 않습니다.',
					login_rate_limited: '접속 시도가 많습니다. 잠시 후 다시 시도해 주세요.',
					chat_rate_limited: '시간당 질문 한도에 도달했습니다. 잠시 후 다시 이용해 주세요.',
					conversation_busy: '이 대화의 답변을 먼저 기다려 주세요.',
					not_found: '이 대화를 찾을 수 없습니다.',
					no_active_qa_index: '검색 자료를 준비하고 있습니다.',
					qa_profile_code_changed: '서비스가 업데이트되었습니다. 새 대화를 시작해 주세요.',
					chat_not_configured: '서비스 접속 설정을 준비하고 있습니다.'
				} as Record<string, string>
			)[error.code] ?? '요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.'
		);
	return '연결을 확인해 주세요. 같은 요청으로 다시 시도할 수 있습니다.';
}
