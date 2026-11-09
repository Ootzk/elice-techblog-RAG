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
export type Identity = { id: string; label: string; role: 'admin' | 'guest' };
export type Session = { csrf_token: string; expires_at: string; identity: Identity };
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
					invalid_access_code: '로그인 코드가 올바르지 않거나 만료·폐기되었습니다.',
					admin_required: '관리자 코드로 로그인해야 합니다.',
					code_cannot_be_extended: '폐기된 코드는 연장할 수 없습니다.',
					admin_code_requires_cli: '관리자 코드는 서버에서 관리합니다.',
					login_rate_limited: '접속 시도가 많습니다. 잠시 후 다시 시도해 주세요.',
					chat_rate_limited: '시간당 질문 한도에 도달했습니다. 잠시 후 다시 이용해 주세요.',
					conversation_busy: '이 대화의 답변을 먼저 기다려 주세요.',
					not_found: '요청한 항목을 찾을 수 없거나 접근 권한이 없습니다.',
					no_active_qa_index: '검색 자료를 준비하고 있습니다.',
					qa_profile_code_changed: '서비스가 업데이트되었습니다. 새 대화를 시작해 주세요.',
					chat_not_configured: '서비스 접속 설정을 준비하고 있습니다.',
					review_tracking_unavailable:
						'MLflow에 연결하지 못했습니다. 저장한 검수 내용은 보존됩니다.',
					review_experiment_not_found: 'MLflow에 평가 실험이 아직 없습니다.',
					review_source_not_ready: '평가 실행이 끝난 뒤 검수할 수 있습니다.',
					review_bundle_unavailable_or_invalid: '검수할 답변 묶음이 없거나 유효하지 않습니다.',
					review_answer_changed: '평가 대상이 일치하지 않습니다. 질문을 다시 불러와 주세요.',
					review_already_submitted: '이미 제출한 평가입니다. 저장된 내용을 다시 불러와 주세요.',
					review_draft_conflict:
						'다른 탭에서 초안을 수정했습니다. 입력을 복사해 둔 뒤 서버 초안을 불러와 주세요.',
					review_no_answers: '이 평가 실행에는 검수 가능한 답변이 없습니다.',
					review_dimensions_mismatch: '모든 평가 항목을 입력해 주세요.',
					review_invalid_applicability: '해당 없음의 적용 조건을 확인해 주세요.',
					reviewer_inactive: '사용 가능한 로그인 코드에만 검수를 배정할 수 있습니다.',
					review_sync_failed:
						'MLflow 동기화에 실패했습니다. 저장된 평가는 보존되며 다시 동기화할 수 있습니다.'
				} as Record<string, string>
			)[error.code] ?? '요청을 처리하지 못했습니다. 잠시 후 다시 시도해 주세요.'
		);
	return '연결을 확인해 주세요. 같은 요청으로 다시 시도할 수 있습니다.';
}
