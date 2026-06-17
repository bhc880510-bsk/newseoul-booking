import warnings

# RuntimeWarning: coroutine '...' was never awaited 경고를 무시하도록 설정
warnings.filterwarnings(
    "ignore",
    message="coroutine '.*' was never awaited",
    category=RuntimeWarning
)
import streamlit as st

st.set_page_config(
    page_title="뉴서울CC 모바일 예약",  # 원하는 앱 제목으로 변경
    page_icon="⛳",  # 이모지(Emoji)를 사용하거나 아래처럼 이미지 파일을 사용합니다.
    layout="wide",  # 앱의 기본 레이아웃을 넓게 설정 (선택 사항)
)
import datetime
import threading
import time
import queue
import sys
import traceback
import requests
import ujson as json
import urllib3
import re
import pytz
import hashlib  # Added import for hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime  # For parsing HTTP Date header

# InsecureRequestWarning 비활성화
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# KST 시간대 객체 전역 정의
KST = pytz.timezone('Asia/Seoul')


# ============================================================
# Utility Functions
# ============================================================

def log_message(message, message_queue):
    """Logs a message with KST timestamp to the queue."""
    try:
        now_kst = datetime.datetime.now(KST)
        timestamp = now_kst.strftime('%H:%M:%S.%f')[:-3]
        message_queue.put(f"UI_LOG:[{timestamp}] {message}")
    except Exception:
        pass


def get_default_date(days):
    """Gets a default date offset by 'days' from today (KST)."""
    return (datetime.datetime.now(KST).date() + datetime.timedelta(days=days))


def format_time_for_api(time_str):
    """Converts HH:MM to HHMM."""
    if not isinstance(time_str, str): time_str = str(time_str)
    time_str = time_str.strip().replace(":", "")
    if re.match(r'^\d{3,4}$', time_str) and time_str.isdigit():
        if len(time_str) == 4:
            return time_str
        elif len(time_str) == 3:
            return f"0{time_str}"
    return "0000"


def format_time_for_display(time_str):
    """Converts HHMM or HH:MM string to HH:MM display format."""
    if not isinstance(time_str, str): time_str = str(time_str)
    time_str = time_str.strip().replace(":", "")
    if re.match(r'^\d{4}$', time_str) and time_str.isdigit():
        return f"{time_str[:2]}:{time_str[2:]}"
    # Handle cases where input might already be HH:MM
    if len(time_str) == 5 and time_str[2] == ':':
        return time_str
    return time_str  # Return original if format is unexpected


def wait_until(target_dt_kst, stop_event, message_queue, log_prefix="프로그램 실행", log_countdown=False):
    """Waits precisely until the target KST datetime."""
    global KST
    log_message(f"⏳ {log_prefix} 대기중: {target_dt_kst.strftime('%H:%M:%S.%f')[:-3]} (KST 기준)", message_queue)
    last_remaining_sec = None
    log_remaining_start = 30  # Start countdown logging 30 seconds before target
    while not stop_event.is_set():
        now_kst = datetime.datetime.now(KST)
        remaining_seconds = (target_dt_kst - now_kst).total_seconds()
        if remaining_seconds <= 0.001:  # Exit if within 1ms or passed
            break
        current_remaining_sec = int(remaining_seconds)
        if log_countdown and remaining_seconds <= log_remaining_start:
            if current_remaining_sec > 0 and current_remaining_sec != last_remaining_sec:
                log_message(f"⏳ 예약 시작 대기중 ({current_remaining_sec}초)", message_queue)
                last_remaining_sec = current_remaining_sec
        # Sleep precisely based on remaining time
        if remaining_seconds < 0.1:
            time.sleep(0.001)  # 1ms sleep
        elif remaining_seconds < 1:
            time.sleep(0.005)  # 5ms sleep
        else:
            time.sleep(0.1)  # 100ms sleep (yield control for UI updates)
    if not stop_event.is_set():
        actual_diff = (datetime.datetime.now(KST) - target_dt_kst).total_seconds()
        log_message(f"✅ 목표 시간 도달! {log_prefix} 스레드 즉시 실행. (종료 시각 차이: {actual_diff:.3f}초)", message_queue)


# ============================================================
# API Booking Core Class (뉴서울CC 전용)
# ============================================================
class APIBookingCore:
    def __init__(self, log_func, message_queue, stop_event):
        self.log_message_func = log_func
        self.message_queue = message_queue
        self.stop_event = stop_event
        self.session = requests.Session()
        self.member_id = None  # Store member_id after login
        # Updated Course mapping for New Seoul CC
        self.course_detail_mapping = {
            "A": "예술OUT", "B": "예술IN", "C": "문화OUT", "D": "문화IN"
        }
        self.proxies = None
        self.KST = pytz.timezone('Asia/Seoul')
        self.API_DOMAIN = "https://www.newseoulgolf.co.kr"
        self.CO_DIV = "204"

        # 2. 핵심 URL 정의
        self.LOGIN_PAGE_URL = f"{self.API_DOMAIN}/mobile/join/login.asp"
        self.INDEX_PAGE_URL = f"{self.API_DOMAIN}/mobile/index.asp"
        self.SESSION_MANAGER_URL = f"{self.API_DOMAIN}/controller/SessionManager.asp"
        self.RESERVATION_CONTROLLER_URL = f"{self.API_DOMAIN}/controller/ReservationController.asp"
        self.LOGIN_URL = f"{self.API_DOMAIN}/controller/MemberController.asp"

    def log_message(self, msg):
        """Logs a message via the provided log function."""
        # Note: log_message_func is defined as log_message(message, message_queue)
        self.log_message_func(msg, self.message_queue)

    # ----------------------------------------------------
    # [오류 수정] 누락된 get_base_headers 헬퍼 함수 추가
    # ----------------------------------------------------
    def get_base_headers(self, referer_url):
        """기본 헤더를 반환하는 헬퍼 함수"""
        return {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.100 Mobile Safari/537.36",
            "Referer": referer_url,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.API_DOMAIN
        }

    # ----------------------------------------------------

    # 3. requests_login 함수를 검증된 로직으로 전면 교체
    def requests_login(self, usrid, usrpass):
        """
        SHA-256 해시와 4단계 쿠키 획득 시퀀스를 사용하여 로그인 세션을 생성하고
        ASPSESSIONIDQEBADTDB 쿠키를 확보합니다.
        """
        # Session setup
        self.session = requests.Session()
        self.session.verify = False  # SSL 검증 비활성화

        # 1. SHA-256 해시
        try:
            hashed_pw = hashlib.sha256(usrpass.encode('utf-8')).hexdigest()
        except Exception as e:
            self.log_message(f"❌ 비밀번호 해시 실패: {e}")
            self.log_message("UI_ERROR:비밀번호 해시 실패!")
            return {'result': 'fail', 'message': 'Password hashing failed'}

        # 2. 기본 헤더 설정 (Referer는 로그인 페이지)
        self.session.headers.update(self.get_base_headers(self.LOGIN_PAGE_URL))

        # --- A. 로그인 전 쿠키 획득 시퀀스 (보조 쿠키 사냥) ---
        try:
            # 2-A. 로그인 페이지 방문 (초기 ASPSESSIONID 획득)
            self.session.get(self.LOGIN_PAGE_URL, timeout=10, verify=False)
            # 2-B. 인덱스 페이지 방문 (추가 쿠키 획득 시도)
            self.session.get(self.INDEX_PAGE_URL, timeout=10, verify=False)
            self.log_message("✅ 초기 쿠키 획득 시퀀스 완료.")
        except requests.RequestException as e:
            self.log_message(f"ℹ️ 초기 페이지 방문 중 오류 발생. (계속 진행): {e}")

        # --- B. 핵심 로그인 POST 요청 ---
        login_data = {"method": "doLogin", "coDiv": self.CO_DIV, "id": usrid,
                      "pw": hashed_pw, "gubun": "1", "check": "N"}  # Hashed PW 사용

        try:
            res = self.session.post(self.LOGIN_URL, data=login_data, timeout=10, verify=False)
            res.raise_for_status()

            # 응답 텍스트 확인 (resultCode: 0000 확인)
            if '"resultCode":"0000"' not in res.text:
                self.log_message(f"❌ 로그인 실패: 서버 응답: {res.text.strip()}")
                self.log_message("UI_ERROR:로그인 실패: 아이디 또는 비밀번호가 일치하지 않습니다.")
                return {'result': 'fail', 'message': 'ID/PW 불일치 또는 서버 오류'}

            self.log_message("✅ 핵심 로그인 POST 성공. ASPSESSIONID 확보.")
            self.member_id = usrid  # 로그인 성공 시 ID 저장
        except requests.RequestException as e:
            self.log_message(f"❌ 네트워크 오류: 로그인 POST 요청 실패: {e}")
            self.log_message("UI_ERROR:로그인 중 네트워크 오류 발생!")
            return {'result': 'fail', 'message': 'Network Error during login'}

        # --- C. 로그인 후 세션 유지 요청 (Referer 변경) ---
        # *** self.get_base_headers 사용 ***
        session_maintain_headers = self.get_base_headers(self.INDEX_PAGE_URL)
        # 주의: 이 요청에만 Referer를 변경하여 사용합니다. (self.session.headers를 직접 업데이트하지 않음)

        session_confirm_data = {'method': 'sessionConfirm', 'path': self.INDEX_PAGE_URL}

        try:
            self.session.post(self.SESSION_MANAGER_URL, headers=session_maintain_headers, data=session_confirm_data,
                              timeout=10, verify=False)
            self.log_message("✅ 세션 유지 요청 완료. (로그인 세션 최종 확정)")
        except requests.RequestException:
            # 이 요청의 실패는 무시하고 다음 단계로 진행
            pass

        return {'result': 'success', 'message': 'Login successful'}

    # 🚨 [요구사항 2] 세션 유지 로직
    def keep_session_alive(self, target_dt):
        """Periodically hits a page to keep the session active until target_dt (1분에 1회)."""
        self.log_message("✅ 세션 유지 스레드 시작.")
        # Use a page that requires login, like the reservation page
        keep_alive_url = f"{self.API_DOMAIN}/member/reser/reser.asp"
        interval_seconds = 60.0  # 1분에 1회

        while not self.stop_event.is_set() and datetime.datetime.now(self.KST) < target_dt:
            try:
                # Use GET request for session keep-alive
                self.session.get(keep_alive_url, timeout=10, verify=False, proxies=self.proxies)
                self.log_message("💚 [세션 유지] 세션 유지 요청 완료.")
            except Exception as e:
                self.log_message(f"❌ [세션 유지] 통신 오류 발생: {e}")

            # Precise wait loop to check stop_event frequently
            start_wait = time.monotonic()
            while time.monotonic() - start_wait < interval_seconds:
                if self.stop_event.is_set() or datetime.datetime.now(self.KST) >= target_dt:
                    break
                time.sleep(1)  # Check stop event every second

        if self.stop_event.is_set():
            self.log_message("🛑 세션 유지 스레드: 중단 신호 감지. 종료합니다.")
        else:
            self.log_message("✅ 세션 유지 스레드: 예약 정시 도달. 종료합니다.")

    # 🚨 [요구사항 1] 서버 시간 동기화 로직
    def get_server_time_offset(self):
        """Fetches server time from HTTP Date header and calculates offset from local KST."""
        # Use the main reservation page which likely returns a Date header
        url = f"{self.API_DOMAIN}/member/reser/reser.asp"
        max_retries = 5
        self.log_message("🔄 뉴서울CC 서버 시간 확인 시도...")
        for attempt in range(max_retries):
            try:
                response = self.session.get(url, timeout=5, verify=False)
                response.raise_for_status()
                server_date_str = response.headers.get("Date")
                if server_date_str:
                    # Parse GMT time from header
                    server_time_gmt = parsedate_to_datetime(server_date_str)
                    # Convert to KST (Server GMT -> KST)
                    server_time_kst = server_time_gmt.astimezone(KST)
                    # Get current local KST time (PC KST)
                    local_time_kst = datetime.datetime.now(KST)
                    # Calculate offset (Server Time - Local Time)
                    time_difference = (server_time_kst - local_time_kst).total_seconds()
                    self.log_message(
                        f"✅ 서버 시간 확인 성공: 서버 KST={server_time_kst.strftime('%H:%M:%S.%f')[:-3]}, 로컬 KST={local_time_kst.strftime('%H:%M:%S.%f')[:-3]}, Offset={time_difference:.3f}초")
                    return time_difference
                else:
                    self.log_message(f"⚠️ 서버 Date 헤더 없음, 재시도 ({attempt + 1}/{max_retries})...")
            except requests.RequestException as e:
                self.log_message(f"⚠️ 서버 시간 요청 실패: {e}, 재시도 ({attempt + 1}/{max_retries})...")
            except Exception as e:
                self.log_message(f"❌ 서버 시간 처리 중 오류: {e}")
                return 0  # Return 0 offset on unexpected error
            time.sleep(0.5)  # Wait before retrying

        self.log_message("❌ 서버 시간 확인 최종 실패. 시간 오차 보정 없이 진행합니다 (Offset=0).")
        return 0  # Default to 0 offset if all retries fail

    # [수정 사항 2] 세션 활성화 함수 (prime_calendar)
    def prime_calendar(self, date_str):
        """Calls getCalendar to set the session's active month."""
        # date_str is "YYYYMMDD", we need "YYYYMM"
        sel_ym = date_str[:6]
        self.log_message(f"🔄 세션 활성화를 위해 {sel_ym}월 달력 정보 로드 시도...")

        url = self.RESERVATION_CONTROLLER_URL  # 수정: __init__에서 정의된 URL 사용
        # network.txt 헤더 참조 (여기서는 별도의 Referer 사용)
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.100 Mobile Safari/537.36",
            "Accept": "*/*",  #
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.API_DOMAIN,
            "Referer": f"{self.API_DOMAIN}/mobile/member/reser/reser.asp",  # 예약 페이지 Referer
        }
        # network.txt 페이로드 참조
        payload = {
            "method": "getCalendar",
            "coDiv": self.CO_DIV,
            "selYm": sel_ym,
            "mCos": "All"
        }

        try:
            # POST 요청 시 headers를 명시적으로 전달 (세션의 헤더를 오버라이드)
            res = self.session.post(url, headers=headers, data=payload, timeout=5.0, verify=False)
            res.raise_for_status()

            # 응답 확인 (JSON 파싱 성공 여부만 확인)
            try:
                json.loads(res.text)
                return True
            except json.JSONDecodeError:
                self.log_message(f"❌ 캘린더 응답 JSON 파싱 실패: {res.text[:50]}...")
                return False

        except requests.RequestException as e:
            self.log_message(f"❌ 캘린더 조회 요청 실패: {e}")
            return False
        except Exception as e:
            self.log_message(f"❌ 캘린더 로드 중 예외 오류: {e}")
            return False

    # 🚨 [추가된 로직] 조회된 코스별 시간대 로그 출력 로직
    def _log_fetched_times_by_course(self, all_times):
        """Logs fetched times grouped by course in the requested format."""

        # 1. Group times by course name (index 3: course_nm, index 0: bk_time_HHMM)
        grouped_times = {}
        for t in all_times:
            course_name = t[3]
            time_hhmm = t[0]
            if course_name not in grouped_times:
                grouped_times[course_name] = set()  # Use set to handle duplicates if any
            grouped_times[course_name].add(time_hhmm)

        # 2. Define a desired sorting order for the courses
        course_order = ["예술OUT", "예술IN", "문화OUT", "문화IN"]

        log_messages = []
        for course_name in course_order:
            if course_name in grouped_times:
                # 3. Sort the unique times (as list)
                times_list = sorted(list(grouped_times[course_name]))

                # 4. Format HHMM to 'HH:MM'
                formatted_times = [f"'{format_time_for_display(t)}'" for t in times_list]

                # 5. Create the log string
                log_string = f"[{course_name}] {', '.join(formatted_times)}"
                log_messages.append(log_string)

        if log_messages:
            self.log_message("📜 **[조회된 코스별 티타임 리스트 (예약 가능/불가능 포함)]**")
            for msg in log_messages:
                self.log_message(msg)

    # 🚨 [요구사항 3] 가동 시작 시간에 도착 후 시간대 가져오기
    def get_all_available_times(self, date):
        """Fetches available tee times for all courses for a given date."""
        self.log_message(f"⏳ {date} 모든 코스 예약 가능 시간대 조회 중...")
        # API endpoint from network logs
        url = self.RESERVATION_CONTROLLER_URL  # 수정: __init__에서 정의된 URL 사용
        headers = {  # Mobile headers
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.100 Mobile Safari/537.36",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": self.API_DOMAIN,
            "Referer": f"{self.API_DOMAIN}/mobile/member/reser/reser.asp",
            "Connection": "keep-alive"
        }

        # [수정] 네트워크 로그에 따라 'cos="All"'로 단일 호출
        all_fetched_times = self._fetch_tee_list(date, "All", headers)

        # Deduplicate results (단일 호출이지만 중복 제거 로직 유지)
        unique_times_map = {}
        for t in all_fetched_times:
            # t = (bk_time, bk_cos, bk_part, course_nm, co_div, status_r) - 6 elements
            time_key = (t[0], t[1], t[2])
            if time_key not in unique_times_map:
                unique_times_map[time_key] = t

        all_fetched_times = list(unique_times_map.values())

        # *** [추가된 로직] 코스별 시간대 로그 출력 ***
        self._log_fetched_times_by_course(all_fetched_times)
        # **********************************************

        self.log_message(f"✅ 총 {len(all_fetched_times)}개의 슬롯 정보 확보 완료 (예약 가능/불가능 포함).")
        return all_fetched_times

    def _fetch_tee_list(self, date, cos, headers, max_retries=2):
        """
        Fetches tee list for a single course.
        """
        url = self.RESERVATION_CONTROLLER_URL  # 수정: __init__에서 정의된 URL 사용
        co_div = self.CO_DIV
        # network.txt 페이로드 참조
        payload = {
            "method": "getTeeList", "coDiv": co_div, "date": date, "cos": cos,  # 'cos'는 "All"로 전달됨
            "roundf": "",  # Keep empty
        }

        for attempt in range(max_retries):
            if self.stop_event.is_set(): return []
            try:
                # POST 요청 시 headers를 명시적으로 전달
                res = self.session.post(url, headers=headers, data=payload, timeout=5.0, verify=False)
                res.raise_for_status()
                data = json.loads(res.text)

                if 'resultCode' in data and data['resultCode'] != '0000':
                    error_msg = data.get('resultMessage', 'API 오류 코드 수신')
                    self.log_message(
                        f"⚠️ _fetch_tee_list API 오류 (cos={cos}, 시도 {attempt + 1}/{max_retries}): {error_msg}")
                    return []  # Don't retry API logic errors

                times = []
                total_rows = data.get('rows', [])

                self.log_message(f"🔍 getTeeList 응답 수신 (cos={cos}): 총 {len(total_rows)}개 슬롯 확인.")

                for t in total_rows:
                    # 예약 가능 여부 필터(t.get('R') == 'OK')를 제거하고, BK_TIME이 존재하는 모든 슬롯을 확보
                    bk_time = format_time_for_api(t.get('BK_TIME'))  # Ensure HHMM format
                    bk_cos = t.get('BK_COS')
                    bk_part = t.get('BK_PART', '1' if bk_cos in ['A', 'C'] else '2')
                    course_nm = self.course_detail_mapping.get(bk_cos, 'Unknown')

                    if bk_time and bk_cos:  # Basic validation
                        # 상태 필드(R)를 포함하여 6개 요소로 구성된 튜플 반환
                        status_r = t.get('R', 'N/A')
                        times.append((bk_time, bk_cos, bk_part, course_nm, co_div, status_r))

                # [수정] 'cos'가 "All"이므로 로그 메시지 간소화
                log_prefix = f"cos={cos}" if cos != "All" else "모든 코스"

                self.log_message(f"🔍 getTeeList 완료 ({log_prefix}): {len(times)}개 슬롯 확보.")
                return times

            except json.JSONDecodeError as e:
                self.log_message(
                    f"❌ _fetch_tee_list JSON 파싱 실패 (cos={cos}, 시도 {attempt + 1}/{max_retries}): {e}. 응답: {res.text[:200]}")
                time.sleep(0.5)  # Wait slightly longer after parsing error
            except requests.RequestException as e:
                self.log_message(f"❌ _fetch_tee_list 네트워크 오류 (cos={cos}, 시도 {attempt + 1}/{max_retries}): {e}")
                time.sleep(0.3)  # Wait before retry
            except Exception as e:
                self.log_message(f"❌ _fetch_tee_list 예외 오류 (cos={cos}, 시도 {attempt + 1}/{max_retries}): {e}")
                break  # Don't retry unexpected errors

        return []  # Return empty list if all retries fail

    # 🚨 [요구사항 3] 시간대 필터링 및 정렬, 상위 5개 로그 출력
    def filter_and_sort_times(self, all_times, start_time_str, end_time_str, target_course_names, is_reverse):
        """
        Filters tee times by time range and course, then sorts.
        """
        start_time_api = format_time_for_api(start_time_str)  # HHMM
        end_time_api = format_time_for_api(end_time_str)  # HHMM

        # Map UI course names ("All", "예술", "문화") to API codes ("A", "B", "C", "D")
        course_map = {"All": ["A", "B", "C", "D"], "예술": ["A", "B"], "문화": ["C", "D"]}
        target_course_codes = set()
        # Handle single or multiple course selections
        course_list = [target_course_names] if isinstance(target_course_names, str) else target_course_names
        for name in course_list:
            target_course_codes.update(course_map.get(name, [name]))

        filtered_times = []
        for t in all_times:
            # t = (bk_time_api, bk_cos_code, bk_part, course_nm, co_div, status_r)
            time_val = t[0]  # HHMM format
            cos_val = t[1]  # API course code (A, B, C, D)
            course_nm_val = t[3]  # Course name for logging
            status_r_val = t[5]  # Status 'OK', 'X', 'N/A'

            # 1. 시간대 및 코스 필터링
            if start_time_api <= time_val <= end_time_api:
                if "All" in target_course_names or cos_val in target_course_codes:
                    # 2. 예약 가능 여부 필터링 (Status 'R' = 'OK'인 경우만 유효한 예약 가능 시간대로 간주)
                    if status_r_val == 'OK':
                        filtered_times.append(t)
                    else:
                        self.log_message(
                            f"ℹ️ 필터링 제외: {course_nm_val} {format_time_for_display(time_val)} (상태: {status_r_val})")

        # Sort by time (primary key), then course code (secondary, optional)
        filtered_times.sort(key=lambda x: (x[0], x[1]), reverse=is_reverse)

        # Log the top 5 filtered/sorted times for user confirmation (요구사항 3)
        formatted_times = [f"{format_time_for_display(t[0])} ({t[3]})" for t in filtered_times]  # Use course_nm (t[3])

        self.log_message(f"🔍 필터링/정렬 완료 (순서: {'역순' if is_reverse else '순차'}) - {len(filtered_times)}개 발견")
        if formatted_times:
            self.log_message("📜 **[최종 예약 우선순위 5개]**")
            for i, time_str in enumerate(formatted_times[:5]):
                self.log_message(f"   {i + 1}순위: {time_str}")
        return filtered_times

    # 🚨 [요구사항 3] 예약 시도 로직
    def try_reservation(self, date, course_code, time_api, cookies):
        """Attempts the actual reservation using the doReservation method."""
        course_name = self.course_detail_mapping.get(course_code, course_code)
        time_display = format_time_for_display(time_api)

        self.log_message(f"🎯 {course_name} 코스 {time_display} 예약 시도 중...")

        url = self.RESERVATION_CONTROLLER_URL  # 수정: __init__에서 정의된 URL 사용
        headers = {  # Mobile headers
            "User-Agent": "Mozilla/5.0 (Linux; Android 10; Mobile AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.6533.100 Mobile Safari/537.36",
            "Referer": f"{self.API_DOMAIN}/mobile/reservation/golf/reservation.asp",  # Mobile Referer
            "Origin": self.API_DOMAIN,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Connection": "keep-alive"
        }
        # Payload meticulously adapted from Tkinter app's try_reservation
        payload = {
            "method": "doReservation",
            "coDiv": self.CO_DIV,
            "day": date,  # YYYYMMDD
            "cos": course_code,  # A, B, C, D
            "time": time_api,  # HHMM
            "roundf": "1",  # Fixed value from Tkinter app
            "charge": "18",  # Fixed value from Tkinter app
            "media": "R",  # Fixed value from Tkinter app (likely means 'Reservation')
            "verify_entity_id": "",  # Empty in Tkinter app
            "verify_entity_ip": "",  # Empty in Tkinter app
            "verify_entity_unique": "MJUViUxhC3DR5ZmXcnTQ/HUNMAqMu6yyOFVH9nlp",  # Static key from Tkinter app
            "member_id": self.member_id  # Use logged-in member ID
        }

        max_retries = 2  # Retry reservation attempt once on network error
        for attempt in range(max_retries):
            if self.stop_event.is_set(): return False
            try:
                res = self.session.post(url, headers=headers, cookies=cookies, data=payload, timeout=10, verify=False)
                res.raise_for_status()
                try:
                    data = res.json()

                    self.log_message(f"✅ 예약 응답 수신: {json.dumps(data, ensure_ascii=False)}")

                    # Check for success indicators based on Tkinter app logic
                    if data.get('resultCode') == '0000' or data.get('R') == 'OK' or data.get('status') == 'success':

                        self.log_message(f"🎉🎉🎉 {course_name} 코스 {time_display} 예약 성공!!! 🎉🎉🎉")

                        # Add post-success delay like in Gold CC app
                        POST_SUCCESS_DELAY = 5

                        self.log_message(f"⏳ 서버 반영 대기: {POST_SUCCESS_DELAY}초...")
                        time.sleep(POST_SUCCESS_DELAY)
                        return True
                    else:
                        # Log specific failure message from API response
                        fail_msg = data.get('resultMessage', data.get('message', '알 수 없는 API 응답'))

                        self.log_message(f"❌ 예약 실패 ({course_name} {time_display}): {fail_msg}")

                        # Don't retry if the API explicitly failed the reservation
                        return False
                except json.JSONDecodeError as e:

                    self.log_message(f"❌ 예약 응답 JSON 파싱 오류: {e}, 응답: {res.text[:200]}...")
                    if attempt < max_retries - 1:
                        time.sleep(0.5);
                        continue
                    else:
                        return False
            except requests.exceptions.Timeout:

                self.log_message(f"❌ 예약 시간 초과 ({course_name} {time_display}), 재시도 ({attempt + 1}/{max_retries})...")
                if attempt < max_retries - 1:
                    time.sleep(0.5);
                    continue
                else:
                    return False
            except requests.exceptions.RequestException as e:

                self.log_message(
                    f"❌ 예약 네트워크 오류 ({course_name} {time_display}): {e}, 재시도 ({attempt + 1}/{max_retries})...")
                if attempt < max_retries - 1:
                    time.sleep(1.0);
                    continue
                else:
                    return False
            except Exception as e:

                self.log_message(f"❌ 예약 중 예외 오류 ({course_name} {time_display}): {e}")
                return False  # Don't retry unknown errors

        return False  # Failed after all retries

    def run_api_booking(self, inputs, sorted_available_times):
        """Attempts reservation on sorted times, up to top 5."""
        if not sorted_available_times:
            self.log_message("❌ 설정된 조건에 맞는 예약 가능 시간대가 없습니다. API 예약 중단.")
            return False

        target_date = inputs['target_date']
        test_mode = inputs.get('test_mode', True)
        cookies = self.session.cookies  # Use current session cookies

        self.log_message(f"🔎 정렬된 시간 순서대로 (상위 {min(5, len(sorted_available_times))}개) 예약 시도...")

        if test_mode:
            # time_info = (bk_time_api, bk_cos_code, bk_part, course_nm, co_div, status_r)
            if not sorted_available_times:  # Check if list is empty
                self.log_message("✅ 테스트 모드: 예약 가능한 시간이 없습니다.")
                return True
            first_time_info = sorted_available_times[0]
            formatted_time = f"{format_time_for_display(first_time_info[0])} ({first_time_info[3]})"  # bk_time, course_nm

            self.log_message(f"✅ 테스트 모드: 1순위 예약 가능 시간 확인: {formatted_time} (실제 예약 시도 안함)")
            return True  # Indicate test mode completion

        # Try booking the top 5 available & filtered times (요구사항 3)
        success = False
        for i, time_info in enumerate(sorted_available_times[:5]):
            if self.stop_event.is_set():
                self.log_message("🛑 예약 시도 중 중단됨.")
                break

            # time_info = (bk_time_api, bk_cos_code, bk_part, course_nm, co_div, status_r)
            bk_time_api = time_info[0]
            bk_cos_code = time_info[1]
            course_nm = time_info[3]
            time_display = format_time_for_display(bk_time_api)

            self.log_message(f"➡️ [시도 {i + 1}/5] 예약 시도: {course_nm} {time_display}")

            success = self.try_reservation(target_date, bk_cos_code, bk_time_api, cookies)

            if success:
                self.log_message(f"🎉🎉🎉 최종 예약 성공!!! [{i + 1}순위] {course_nm} {time_display}")
                break  # Exit loop on first success

        if not success and not self.stop_event.is_set():
            self.log_message(f"❌ 상위 {min(5, len(sorted_available_times))}개 시간대 예약 시도 실패.")

        return success


# ============================================================
# Main Threading Logic - start_pre_process
# ============================================================
def start_pre_process(message_queue, stop_event, inputs):
    """Main background thread function orchestrating the booking process."""
    global KST
    log_message("[INFO] ⚙️ 예약 시작 조건 확인 완료.", message_queue)
    try:
        core = APIBookingCore(log_message, message_queue, stop_event)

        # 1. Login
        log_message("✅ 작업 진행 중: API 로그인 시도...", message_queue)
        login_result = core.requests_login(inputs['id'], inputs['pw'])
        if login_result['result'] != 'success':
            return  # Stop processing if login fails
        log_message("✅ 로그인 성공.", message_queue)

        # 2. Calculate Target Time (KST-aware)
        run_dt_str = f"{inputs['run_date']} {inputs['run_time']}"
        run_dt_naive = datetime.datetime.strptime(run_dt_str, '%Y%m%d %H:%M:%S')
        run_dt_kst = KST.localize(run_dt_naive)  # Make it timezone-aware

        # 3. Start Session Keep-Alive Thread (요구사항 2)
        session_thread = threading.Thread(target=core.keep_session_alive, args=(run_dt_kst,), daemon=True)
        session_thread.start()
        log_message("✅ 세션 유지 스레드 백그라운드 시작.", message_queue)

        # 4. Time Synchronization Wait (정시 30초 전)
        sync_check_time = run_dt_kst - datetime.timedelta(seconds=30)
        now_kst = datetime.datetime.now(KST)

        # 4-1. 서버 시간 동기화 시점까지 대기 (30초 전)
        if now_kst < sync_check_time:
            wait_until(sync_check_time, stop_event, message_queue, "서버 시간 동기화")

        time_offset = 0  # Default offset
        target_local_time_kst = run_dt_kst  # Default to run_dt_kst before offset application

        # 4-2. 서버 시간 오차 확인 (30초 전에 실행)
        if not stop_event.is_set():
            time_offset = core.get_server_time_offset()  # 서버 시간 가져와 오차 계산
            # 서버 시간 오차를 반영하여 최종 로컬 대기 시간을 계산
            target_local_time_kst = run_dt_kst - datetime.timedelta(seconds=time_offset)
            log_message(f"🎯 예약 시도 목표 시간 (로컬 KST 기준): {target_local_time_kst.strftime('%H:%M:%S.%f')[:-3]}",
                        message_queue)
        else:
            return  # Stop if interrupted during sync wait

        # ********** 🚨 [핵심 수정: 캘린더 로드 시점 변경] 🚨 **********
        # 5. Session Prime: getCalendar 호출 (정시 도착 이전에 완료하여 지연 제거)
        log_message("🔎 **[선행 작업]** 달력 정보 로드 (세션 활성화)...", message_queue)
        calendar_primed = core.prime_calendar(inputs['target_date'])
        if not calendar_primed:
            log_message("⚠️ 달력 정보 로드에 실패했습니다. 티 타임 조회가 실패할 수 있습니다.", message_queue)
        else:
            log_message("✅ **[선행 작업]** 달력 로드 완료. 세션 활성화.", message_queue)

        if stop_event.is_set(): return
        # *************************************************************

        # 6. Wait until the adjusted target time (요구사항 1 & 3)
        # 조정된 목표 시간까지 대기. 마지막 30초부터 '예약 시작 대기중' 카운트다운 표시
        now_kst = datetime.datetime.now(KST)
        if now_kst < target_local_time_kst:
            wait_until(target_local_time_kst, stop_event, message_queue, "최종 예약 시도", log_countdown=True)
        else:
            log_message(f"⚠️ [지연 감지] 현재시간이 목표시간보다 늦어 즉시 예약 시도됩니다.", message_queue)

        if stop_event.is_set():
            log_message("🛑 대기 중 중단 신호 수신.", message_queue)
            return

        # 7. Fetch, Filter, Sort Tee Times (정시 직후 실행)
        log_message("🔎 🚀 **[골든 타임]** 티 타임 조회 시작...", message_queue)
        all_times = core.get_all_available_times(inputs['target_date'])

        if stop_event.is_set(): return  # Check again after fetching times

        log_message(
            f"🔎 필터링 조건: {inputs['start_time']}~{inputs['end_time']}, 코스: {inputs['course_type']}, 순서: {inputs['order']}",
            message_queue)
        sorted_times = core.filter_and_sort_times(
            all_times, inputs['start_time'], inputs['end_time'],
            inputs['target_course'], inputs['reverse_order']
        )

        # 8. Apply Booking Delay (예약 지연) (요구사항 1: 티타임 조회 및 정렬 후 적용)
        try:
            delay_seconds = float(inputs.get('delay', 0))
            if delay_seconds > 0:
                log_message(f"⏳ 설정된 예약 지연 ({delay_seconds}초) 적용...", message_queue)
                time.sleep(delay_seconds)
                log_message("✅ 지연 시간 대기 완료.", message_queue)
        except ValueError:
            log_message("⚠️ 예약 지연 시간 값이 잘못되어 지연 없이 진행합니다.", message_queue)

        if stop_event.is_set(): return  # Check again after delay

        # 9. Attempt Booking (요구사항 3)
        log_message("[API EXEC] 🚀 API 예약 프로세스 시작!", message_queue)
        success = core.run_api_booking(inputs, sorted_times)

        if success:
            log_message("✅ 예약 프로세스 성공적으로 완료.", message_queue)
        else:
            log_message("❌ 예약 프로세스 실패.", message_queue)
            message_queue.put("UI_ERROR:예약에 실패했습니다. 로그를 확인하세요.")  # Send error to UI

    except Exception as e:
        # Log unexpected errors
        exc_type, exc_value, exc_traceback = sys.exc_info()
        traceback_details = traceback.format_exception(exc_type, exc_value, exc_traceback)
        error_msg = f"❌ 치명적인 오류 발생: {exc_value}\n{''.join(traceback_details)}"
        log_message(error_msg, message_queue)
        message_queue.put(f"UI_ERROR:치명적 오류 발생! 로그 확인: {exc_value}")
    finally:
        log_message("ℹ️ 예약 프로세스 스레드 종료.", message_queue)
        # Ensure stop_event is set on thread exit, regardless of success/fail
        stop_event.set()


# ============================================================
# Streamlit UI
# ============================================================

# Initialize Session State Variables
if 'log_messages' not in st.session_state:
    st.session_state.log_messages = ["프로그램 실행 준비 완료."]
if 'is_running' not in st.session_state:
    st.session_state.is_running = False
if 'stop_event' not in st.session_state:
    st.session_state.stop_event = threading.Event()
if 'booking_thread' not in st.session_state:
    st.session_state.booking_thread = None
if 'message_queue' not in st.session_state:
    st.session_state.message_queue = queue.Queue()
if 'inputs' not in st.session_state:
    st.session_state.inputs = {}
if 'run_id' not in st.session_state:
    st.session_state.run_id = None
if 'log_container_placeholder' not in st.session_state:
    st.session_state.log_container_placeholder = None
if '_button_clicked_status_change' not in st.session_state:
    st.session_state['_button_clicked_status_change'] = False

# Default Input Values
if 'id_input' not in st.session_state: st.session_state.id_input = ""
if 'pw_input' not in st.session_state: st.session_state.pw_input = ""
if 'date_input' not in st.session_state:
    today = datetime.datetime.now(KST)
    next_month_first_day = (today.replace(day=1) + datetime.timedelta(days=32)).replace(day=1)
    next_month_last_day = (next_month_first_day.replace(day=28) + datetime.timedelta(days=4)).replace(
        day=1) - datetime.timedelta(days=1)
    default_booking_day = min(today.day, next_month_last_day.day)
    st.session_state.date_input = next_month_first_day.replace(day=default_booking_day).date()
if 'run_date_input' not in st.session_state:
    st.session_state.run_date_input = get_default_date(0).strftime('%Y%m%d')  # Today
if 'run_time_input' not in st.session_state:
    st.session_state.run_time_input = "10:00:00"  # Default 10:00:00 KST

# ----------------- [수정된 부분] -----------------
if 'res_start_input' not in st.session_state:
    st.session_state.res_start_input = "07:30"  # Default 07:30 (수정됨)
if 'res_end_input' not in st.session_state:
    st.session_state.res_end_input = "09:00"  # Default 09:00 (유지됨)
# -------------------------------------------------

if 'course_input' not in st.session_state:
    st.session_state.course_input = "All"  # Default All courses
if 'order_input' not in st.session_state:
    st.session_state.order_input = "역순(▼)"  # Default to Reverse order
if 'delay_input' not in st.session_state:
    st.session_state.delay_input = "1.0"  # Default delay
if 'test_mode_checkbox' not in st.session_state:
    st.session_state.test_mode_checkbox = True  # Default to Test Mode ON


# --- Callback Functions ---
def stop_booking():
    """Callback for the '중단/취소' button."""
    if not st.session_state.is_running: return
    log_message("🛑 사용자가 '취소' 버튼을 클릭했습니다. 프로세스 종료 중...", st.session_state.message_queue)
    st.session_state.stop_event.set()
    st.session_state.is_running = False
    st.session_state.run_id = None
    st.session_state._button_clicked_status_change = True  # Signal state change


def run_booking():
    """Callback for the '예약 시작' button."""
    if st.session_state.is_running:
        st.error("⚠️ 이미 예약 스레 실행 중입니다.")
        return

    # 1. Validate Inputs First
    try:
        datetime.datetime.strptime(st.session_state.run_date_input, '%Y%m%d')
        datetime.datetime.strptime(st.session_state.run_time_input, '%H:%M:%S')
        start_t = datetime.datetime.strptime(st.session_state.res_start_input, '%H:%M')
        end_t = datetime.datetime.strptime(st.session_state.res_end_input, '%H:%M')
        if start_t >= end_t:
            st.error("⚠️ 시작 시간이 종료 시간보다 같거나 늦을 수 없습니다.")
            return
        delay_val = float(st.session_state.delay_input)
        if delay_val < 0:
            st.error("⚠️ 예약 지연 시간은 0 이상이어야 합니다.")
            return
    except ValueError as e:
        st.error(f"⚠️ 입력 값 오류: {e}")
        return

    # 2. Set Running State & Clear Queue/Logs
    st.session_state.is_running = True
    st.session_state.stop_event.clear()
    st.session_state.log_messages = ["🔄 예약 프로세스 시작 중..."]  # Clear previous logs
    st.session_state.run_id = time.time()
    st.session_state._button_clicked_status_change = True  # Signal state change

    # Clear message queue
    while not st.session_state.message_queue.empty():
        try:
            st.session_state.message_queue.get_nowait()
        except queue.Empty:
            break

    # 3. Prepare Inputs Dictionary for the Thread
    st.session_state.inputs = {
        'id': st.session_state.id_input,
        'pw': st.session_state.pw_input,
        'target_date': st.session_state.date_input.strftime('%Y%m%d'),  # API needs YYYYMMDD
        'run_date': st.session_state.run_date_input,  # YYYYMMDD string
        'run_time': st.session_state.run_time_input,  # HH:MM:SS string
        'start_time': st.session_state.res_start_input,  # HH:MM string
        'end_time': st.session_state.res_end_input,  # HH:MM string
        'course_type': st.session_state.course_input,  # "All", "예술", "문화"
        'order': st.session_state.order_input,  # "순차(▲)", "역순(▼)"
        'delay': st.session_state.delay_input,  # Delay string (float convertible)
        'test_mode': st.session_state.test_mode_checkbox,  # Boolean
        'target_course': ["예술", "문화"] if st.session_state.course_input == "All" else [st.session_state.course_input],
        'reverse_order': st.session_state.order_input == "역순(▼)"  # Boolean
    }

    # 4. Start the Background Thread
    st.session_state.booking_thread = threading.Thread(
        target=start_pre_process,
        args=(st.session_state.message_queue, st.session_state.stop_event, st.session_state.inputs),
        daemon=True
    )
    st.session_state.booking_thread.start()


# --- Real-time Update Function ---
def check_queue_and_rerun():
    """Checks the message queue and triggers rerun if needed."""
    if st.session_state.run_id is None: return

    new_message_received = False
    is_running_before_check = st.session_state.is_running
    ui_error_occurred = False  # Flag to check if UI error stopped the process

    # Process all messages in the queue
    while not st.session_state.message_queue.empty():
        try:
            message = st.session_state.message_queue.get_nowait()
        except queue.Empty:
            break

        if message.startswith("UI_ERROR:"):
            error_text = message.replace("UI_ERROR:", "[UI ALERT] ❌ ")
            st.session_state.log_messages.append(error_text)
            st.session_state.is_running = False  # Stop on error
            st.session_state.stop_event.set()  # Signal thread to stop
            st.session_state.run_id = None
            new_message_received = True
            ui_error_occurred = True  # Mark that an error stopped the process
            break  # Stop processing messages on UI error
        elif message.startswith("UI_LOG:"):
            log_text = message.replace("UI_LOG:", "")
            st.session_state.log_messages.append(log_text)
            new_message_received = True

    # Check if the thread has finished on its own (only if no UI error occurred)
    if is_running_before_check and not ui_error_occurred:
        if st.session_state.booking_thread and not st.session_state.booking_thread.is_alive():
            # Thread finished without explicit UI error or stop button
            st.session_state.is_running = False
            st.session_state.run_id = None
            new_message_received = True  # Ensure rerun to update button state

    # Rerun if new messages arrived OR if the process finished/stopped
    if new_message_received:
        st.rerun()
        return

    # If still running and no new messages, schedule next check/rerun
    if st.session_state.is_running and st.session_state.run_id is not None:
        time.sleep(0.1)  # Short delay to prevent excessive reruns
        st.rerun()  # Trigger rerun for continuous log checking


# ============================================================
# UI 레이아웃 (원본 스타일 유지)
# ============================================================

st.set_page_config(layout="wide", menu_items=None)

# --- CSS Styling (원본 유지) ---
st.markdown("""
<style>
    /* Reset margins/padding */
    div[data-testid="stAppViewContainer"] > section,
    div[data-testid="stVerticalBlock"] { margin-top: 0px !important; padding-top: 0px !important; }
    .main > div { padding-top: 0rem !important; }

    /* Title Styling */
    .app-title {
        font-size: 26px !important; 
        font-weight: bold;
        margin-top: 10px !important; 
        margin-bottom: 15px !important; 
        text-align: center; 
    }

    /* Input Width Control */
    div[data-testid="stTextInput"],
    div[data-testid="stDateInput"],
    div[data-testid="stSelectbox"] {
        max-width: 220px !important; 
    }

    /* Section Header Styling */
    .section-header {
        font-size: 16px;
        font-weight: bold;
        margin-top: 10px; 
        margin-bottom: 5px; 
    }

    /* Center Align Containers */
    div[data-testid="stVerticalBlock"] > div:nth-child(1) > div:nth-child(1) > div {
        max-width: 500px; 
        margin: 0 auto !important; 
    }
    div[data-testid="stVerticalBlock"] > div:nth-child(3) {
        max-width: 350px; 
        margin: 0 auto !important; 
    }
     div[data-testid="stVerticalBlock"] > div:nth-child(3) button {
        width: 100%;
    }
     div[data-testid="stVerticalBlock"] > div:nth-child(5) {
        max-width: 600px; 
        margin: 0 auto !important; 
    }

</style>
""", unsafe_allow_html=True)

# Language tag injection for browser translation issue
st.markdown(
    """
    <html lang="ko">
    <head>
        <meta charset="UTF-8">
    </head>
    <body>
    """,
    unsafe_allow_html=True
)

# --- Title ---
st.markdown('<p class="app-title">⛳ 뉴서울CC 모바일 예약</p>', unsafe_allow_html=True)

# --- 1. Settings Section ---
with st.container(border=True):
    st.markdown('<p class="section-header">🔑 로그인 및 조건 설정</p>', unsafe_allow_html=True)

    # 1-1. Login Credentials
    col1, col2 = st.columns(2)
    with col1:
        st.text_input("사용자ID", key="id_input", disabled=st.session_state.is_running)
    with col2:
        st.text_input("암호", type="password", key="pw_input", disabled=st.session_state.is_running)

    # 1-2. Booking & Execution Time
    st.markdown("---")  # Separator
    st.markdown('<p class="section-header">🗓️ 예약/가동 시간 설정</p>', unsafe_allow_html=True)
    col3, col4, col5 = st.columns([1, 1, 1])
    with col3:
        st.date_input("예약일", key="date_input", format="YYYY-MM-DD", disabled=st.session_state.is_running)
    with col4:
        st.text_input("가동시작일 (YYYYMMDD)", key="run_date_input", help="API 실행 기준 날짜",
                      disabled=st.session_state.is_running)
    with col5:
        st.text_input("가동시작시간 (HH:MM:SS)", key="run_time_input", help="API 실행 기준 시간 (KST)")

    # 1-3. Filters & Priority
    st.markdown("---")  # Separator
    st.markdown('<p class="section-header">⚙️ 티타임 필터 및 우선순위</p>', unsafe_allow_html=True)
    col6, col7, col8 = st.columns([2.5, 2.5, 1.5])
    with col6:

        # ----------------- [수정된 부분] -----------------
        # 🚨 [UI 수정 요청사항] 시작시간: 06:00 ~ 14:00까지 30분 단위 생성
        start_time_options = [f"{h:02d}:{m:02d}" for h in range(6, 15) for m in (0, 30) if not (h == 14 and m == 30)]
        # 기본값('07:30')이 옵션에 있는지 확인하고 index 설정
        try:
            default_start_time_index = start_time_options.index(st.session_state.res_start_input)
        except ValueError:
            default_start_time_index = 3  # 기본값이 목록에 없으면 '07:30'을 기본으로 설정 (인덱스 3)
        # -------------------------------------------------

        st.selectbox(
            "시작시간 (HH:MM)",
            options=start_time_options,
            # index=default_start_time_index,
            key="res_start_input",
            disabled=st.session_state.is_running
        )

        # 🚨 [UI 수정 요청사항] 종료시간: st.text_input -> st.selectbox 변경 (기존 로직 유지)
        end_time_options = [f"{h:02d}:{m:02d}" for h in range(7, 17) for m in (0, 30) if not (h == 17 and m == 30)]
#        end_time_options = [f"{h:02d}:00" for h in range(7, 18)]  # 07:00 ~ 17:00
        # 기본값('09:00')이 옵션에 있는지 확인하고 index 설정
        try:
            default_end_time_index = end_time_options.index(st.session_state.res_end_input)
        except ValueError:
            default_end_time_index = 2  # 기본값이 목록에 없으면 '09:00'을 기본으로
        st.selectbox(
            "종료시간 (HH:MM)",
            options=end_time_options,
            # index=default_end_time_index,
            key="res_end_input",
            disabled=st.session_state.is_running
        )

    with col7:
        st.selectbox("코스선택", ["All", "예술", "문화"], key="course_input", disabled=st.session_state.is_running)
        st.selectbox("예약순서", ["역순(▼)", "순차(▲)"], key="order_input", disabled=st.session_state.is_running)
    with col8:
        st.text_input("예약지연(초)", key="delay_input", help="목표 시간 도달 후 추가 대기 시간(초)", disabled=st.session_state.is_running)
        st.checkbox("테스트 모드", key="test_mode_checkbox", help="실제 예약 실행 안함", disabled=st.session_state.is_running)

# --- 2. Action Buttons ---
st.markdown("---")  # Separator
# **[오류 수정]** 3개의 컬럼을 생성했으므로 3개의 변수에 언패킹해야 합니다.
col_start, col_stop, col_spacer = st.columns([1.5, 1.5, 5])
with col_start:
    st.button("🚀 예약 시작", on_click=run_booking, disabled=st.session_state.is_running, type="primary")
with col_stop:
    st.button("❌ 취소", on_click=stop_booking, disabled=not st.session_state.is_running, type="secondary")
# col_spacer는 빈 공간으로 사용됨

# --- 3. Log Section ---
st.markdown("---")  # Separator
st.markdown('<p class="section-header">📝 실행 로그</p>', unsafe_allow_html=True)

# Placeholder for log container
if st.session_state.log_container_placeholder is None:
    st.session_state.log_container_placeholder = st.empty()

# Display logs within the container
with st.session_state.log_container_placeholder.container(height=250):  # Fixed height
    for msg in reversed(st.session_state.log_messages[-500:]):
        safe_msg = msg.replace("<", "&lt;").replace(">", "&gt;")
        color = "black"
        if "[UI ALERT]" in msg:
            color = "red"
        elif "🎉" in msg or "✅" in msg and "대기중" not in msg:
            color = "green"
        elif "💚 [세션 유지]" in msg or "📜" in msg:
            color = "#008080"  # Teal color for session/list logs
        st.markdown(f'<p style="font-size: 11px; margin: 0px; color: {color}; font-family: monospace;">{safe_msg}</p>',
                    unsafe_allow_html=True)

# --- Start the real-time update loop ---
check_queue_and_rerun()

# --- Rerun handling after button click ---
if st.session_state.get('_button_clicked_status_change', False):
    st.session_state['_button_clicked_status_change'] = False
    st.rerun()
