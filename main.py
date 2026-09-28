import re
import json
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import streamlit as st


# =========================================================
# 기본 설정
# =========================================================

st.set_page_config(
    page_title="NEIS 급식 검색",
    page_icon="🍱",
    layout="wide",
)

API_URL = "https://open.neis.go.kr/hub"

# 한국 시간
KST = timezone(timedelta(hours=9))

# 시도교육청 코드
EDU_CODES = {
    "서울": "B10",
    "부산": "C10",
    "대구": "D10",
    "인천": "E10",
    "광주": "F10",
    "대전": "G10",
    "울산": "H10",
    "세종": "I10",
    "경기": "J10",
    "강원": "K10",
    "충북": "M10",
    "충남": "N10",
    "전북": "P10",
    "전남": "Q10",
    "경북": "R10",
    "경남": "S10",
    "제주": "T10",
}

# 학교 이름 축약어 → 정식 명칭 검색용 단어
SCHOOL_ABBREVIATIONS = {
    "여고": "여자고등학교",
    "남고": "남자고등학교",
    "여중": "여자중학교",
    "남중": "남자중학교",
    "초": "초등학교",
    "중": "중학교",
    "고": "고등학교",
}

# 알레르기 번호
ALLERGENS = {
    "1": "난류",
    "2": "우유",
    "3": "메밀",
    "4": "땅콩",
    "5": "대두",
    "6": "밀",
    "7": "고등어",
    "8": "게",
    "9": "새우",
    "10": "돼지고기",
    "11": "복숭아",
    "12": "토마토",
    "13": "아황산류",
    "14": "호두",
    "15": "닭고기",
    "16": "쇠고기",
    "17": "오징어",
    "18": "조개류",
    "19": "잣",
}


# =========================================================
# NEIS API 함수
# =========================================================

def get_api_key():
    """Streamlit Secrets에서 NEIS API 인증키를 읽는다."""
    try:
        key = st.secrets["NEIS_KEY"]
    except Exception:
        return None

    if not key:
        return None

    return str(key).strip()


def call_neis(service, params):
    """
    NEIS Open API를 JSON으로 호출한다.
    requests를 사용하지 않고 urllib을 사용한다.
    """

    api_key = get_api_key()

    if not api_key:
        return None, "NEIS_KEY가 설정되지 않았습니다."

    query = {
        "KEY": api_key,
        "Type": "json",
        "pIndex": 1,
        "pSize": 1000,
    }

    query.update(params)

    url = f"{API_URL}/{service}?{urllib.parse.urlencode(query)}"

    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            raw = response.read().decode("utf-8")

        data = json.loads(raw)

    except Exception as e:
        return None, f"NEIS API 호출 중 오류가 발생했습니다: {e}"

    # NEIS가 ERROR 구조를 반환하는 경우
    if isinstance(data, dict) and "RESULT" in data:
        result = data["RESULT"]

        if isinstance(result, dict):
            code = result.get("CODE", "")
            message = result.get("MESSAGE", "")

            if code != "INFO-000":
                return None, f"NEIS API 오류: {code} - {message}"

    return data, None


def extract_rows(data, service):
    """NEIS JSON에서 실제 데이터 행만 추출한다."""

    if not data:
        return []

    rows = []

    if service in data:
        service_data = data[service]

        if isinstance(service_data, list):
            for block in service_data:
                if isinstance(block, dict) and "row" in block:
                    rows.extend(block["row"])

    return rows


# =========================================================
# 학교 검색
# =========================================================

def normalize_school_query(query):
    """
    수도여고 → 수도여자고등학교
    수도고 → 수도고등학교
    같은 축약 검색을 지원한다.
    """

    query = query.strip().replace(" ", "")

    if not query:
        return ""

    # 이미 정식 학교명이 포함되어 있으면 그대로 사용
    if any(
        word in query
        for word in ["초등학교", "중학교", "고등학교", "학교"]
    ):
        return query

    # 가장 긴 축약어부터 처리
    for short, full in sorted(
        SCHOOL_ABBREVIATIONS.items(),
        key=lambda x: len(x[0]),
        reverse=True,
    ):
        if query.endswith(short):
            prefix = query[:-len(short)]
            return prefix + full

    return query


@st.cache_data(ttl=3600, show_spinner=False)
def search_schools(query, region):
    """
    학교기본정보 API를 이용하여 학교를 검색한다.

    지역을 알고 있으면 해당 교육청을 우선 사용한다.
    """

    query = normalize_school_query(query)

    if not query:
        return pd.DataFrame()

    region_code = EDU_CODES.get(region)

    params = {
        "SCHUL_NM": query,
    }

    if region_code:
        params["ATPT_OFCDC_SC_CODE"] = region_code

    data, error = call_neis("SchoolInfo", params)

    if error:
        return pd.DataFrame()

    rows = extract_rows(data, "SchoolInfo")

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)

    wanted = [
        "ATPT_OFCDC_SC_CODE",
        "ATPT_OFCDC_SC_NM",
        "SD_SCHUL_CODE",
        "SCHUL_NM",
        "ORG_RDNMA",
        "ORG_RDNDA",
        "SCHUL_KND_SC_NM",
    ]

    existing = [col for col in wanted if col in df.columns]

    return df[existing].drop_duplicates().reset_index(drop=True)


# =========================================================
# 급식 조회
# =========================================================

@st.cache_data(ttl=1800, show_spinner=False)
def get_meal_data(edu_code, school_code, start_date, end_date):
    """
    지정한 학교의 기간별 급식 데이터를 가져온다.
    """

    params = {
        "ATPT_OFCDC_SC_CODE": edu_code,
        "SD_SCHUL_CODE": school_code,
        "MLSV_FROM_YMD": start_date.strftime("%Y%m%d"),
        "MLSV_TO_YMD": end_date.strftime("%Y%m%d"),
    }

    data, error = call_neis("mealServiceDietInfo", params)

    if error:
        return pd.DataFrame()

    rows = extract_rows(data, "mealServiceDietInfo")

    if not rows:
        return pd.DataFrame()

    return pd.DataFrame(rows)


def get_selected_day_meal(df, selected_date):
    """선택한 날짜의 중식만 가져온다."""

    if df.empty:
        return pd.DataFrame()

    target = selected_date.strftime("%Y%m%d")

    result = df[
        (df["MLSV_YMD"].astype(str) == target)
        & (df["MMEAL_SC_NM"].astype(str).str.contains("중식", na=False))
    ].copy()

    return result


# =========================================================
# 메뉴 / 알레르기 처리
# =========================================================

def clean_menu_text(text):
    """메뉴 문자열에서 HTML 태그 등을 정리한다."""

    if pd.isna(text):
        return ""

    text = str(text)

    text = text.replace("<br/>", "\n")
    text = text.replace("<br>", "\n")
    text = text.replace("<BR/>", "\n")
    text = text.replace("<BR>", "\n")

    # 혹시 남아 있는 HTML 태그 제거
    text = re.sub(r"<[^>]+>", "", text)

    return text.strip()


def extract_allergen_numbers(menu_text):
    """메뉴명 뒤에 붙은 알레르기 번호를 추출한다."""

    numbers = set()

    for match in re.findall(r"\(([\d., ]+)\)", menu_text):
        for number in re.findall(r"\d+", match):
            if number in ALLERGENS:
                numbers.add(number)

    return sorted(
        numbers,
        key=lambda x: int(x),
    )


def remove_allergen_numbers(menu_text):
    """메뉴 표시용으로 알레르기 번호 괄호를 제거한다."""

    return re.sub(
        r"\s*\([\d., ]+\)",
        "",
        menu_text,
    ).strip()


# =========================================================
# 단백질 파싱
# =========================================================

def parse_protein(ntr_info):
    """
    NTR_INFO에서 단백질(g)을 숫자로 추출한다.

    예:
    탄수화물(g) : 80.3
    단백질(g) : 25.7
    지방(g) : 18.4
    """

    if pd.isna(ntr_info):
        return None

    text = str(ntr_info)

    # 단백질(g) : 25.7
    patterns = [
        r"단백질\s*\(g\)\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)",
        r"단백질\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)",
        r"단백질\s*\(g\)\s*([0-9]+(?:\.[0-9]+)?)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            try:
                return float(match.group(1))
            except ValueError:
                pass

    return None


def prepare_protein_data(meal_df):
    """최근 두 달 급식의 단백질 데이터를 요일별로 집계한다."""

    if meal_df.empty:
        return pd.DataFrame()

    df = meal_df.copy()

    if "MLSV_YMD" not in df.columns:
        return pd.DataFrame()

    # 날짜
    df["날짜"] = pd.to_datetime(
        df["MLSV_YMD"].astype(str),
        format="%Y%m%d",
        errors="coerce",
    )

    # 중식만 사용
    if "MMEAL_SC_NM" in df.columns:
        df = df[
            df["MMEAL_SC_NM"].astype(str).str.contains(
                "중식",
                na=False,
            )
        ]

    # 단백질 파싱
    if "NTR_INFO" not in df.columns:
        return pd.DataFrame()

    df["단백질(g)"] = df["NTR_INFO"].apply(parse_protein)

    df = df.dropna(
        subset=["날짜", "단백질(g)"]
    )

    if df.empty:
        return pd.DataFrame()

    # 요일
    weekday_map = {
        0: "월요일",
        1: "화요일",
        2: "수요일",
        3: "목요일",
        4: "금요일",
        5: "토요일",
        6: "일요일",
    }

    df["요일"] = df["날짜"].dt.weekday.map(weekday_map)

    # 평일만 사용
    df = df[df["날짜"].dt.weekday < 5]

    weekday_order = [
        "월요일",
        "화요일",
        "수요일",
        "목요일",
        "금요일",
    ]

    result = (
        df.groupby("요일", as_index=False)["단백질(g)"]
        .mean()
    )

    result["요일"] = pd.Categorical(
        result["요일"],
        categories=weekday_order,
        ordered=True,
    )

    result = (
        result
        .sort_values("요일")
        .reset_index(drop=True)
    )

    return result


# =========================================================
# 화면
# =========================================================

st.title("🍱 NEIS 급식 검색")

st.caption(
    "나이스(NEIS) Open API를 이용해 학교 급식과 영양정보를 조회합니다."
)

# ---------------------------------------------------------
# API 키 확인
# ---------------------------------------------------------

if not get_api_key():
    st.error(
        "NEIS API 인증키가 없습니다. "
        "Streamlit Cloud의 Secrets에 `NEIS_KEY`를 등록해 주세요."
    )

    st.code(
        '[secrets]\nNEIS_KEY = "발급받은_인증키"',
        language="toml",
    )

    st.info(
        "NEIS 교육정보 개방 포털에서 Open API 인증키를 신청할 수 있습니다."
    )

    st.stop()


# ---------------------------------------------------------
# 학교 검색
# ---------------------------------------------------------

st.header("1. 학교 찾기")

col1, col2 = st.columns([2, 1])

with col1:
    school_query = st.text_input(
        "학교 이름",
        placeholder="예: 수도여고, 서울 수도여고",
    )

with col2:
    region_options = ["전체"] + list(EDU_CODES.keys())

    selected_region = st.selectbox(
        "지역",
        region_options,
    )


search_clicked = st.button(
    "🔎 학교 검색",
    type="primary",
    use_container_width=True,
)


if search_clicked:
    if not school_query.strip():
        st.warning("학교 이름을 입력해 주세요.")

    else:
        # 지역명이 검색어에 들어 있으면 자동으로 지역 추정
        detected_region = selected_region

        for region in EDU_CODES:
            if region in school_query:
                detected_region = region
                break

        # "서울 수도여고" → "수도여고"처럼 지역명 제거
        cleaned_query = school_query.strip()

        for region in EDU_CODES:
            cleaned_query = cleaned_query.replace(region, "")

        cleaned_query = cleaned_query.strip()

        normalized_query = normalize_school_query(
            cleaned_query
        )

        with st.spinner("학교를 검색하고 있습니다..."):
            result = search_schools(
                normalized_query,
                None if detected_region == "전체" else detected_region,
            )

        if result.empty:
            st.warning(
                f"'{school_query}'에 해당하는 학교를 찾지 못했습니다."
            )

        else:
            st.session_state["school_results"] = result


# ---------------------------------------------------------
# 검색 결과
# ---------------------------------------------------------

if "school_results" in st.session_state:

    school_df = st.session_state["school_results"]

    st.subheader("검색 결과")

    options = []

    for _, row in school_df.iterrows():

        school_name = str(
            row.get("SCHUL_NM", "")
        )

        region_name = str(
            row.get("ATPT_OFCDC_SC_NM", "")
        )

        address = str(
            row.get("ORG_RDNMA", "")
        )

        label = f"{school_name} · {region_name}"

        if address and address != "nan":
            label += f" · {address}"

        options.append(label)

    selected_index = st.selectbox(
        "학교를 선택하세요",
        range(len(options)),
        format_func=lambda i: options[i],
    )

    selected_school = school_df.iloc[selected_index]

    st.session_state["selected_school"] = selected_school


# ---------------------------------------------------------
# 선택 학교
# ---------------------------------------------------------

if "selected_school" in st.session_state:

    school = st.session_state["selected_school"]

    school_name = str(school["SCHUL_NM"])
    edu_code = str(school["ATPT_OFCDC_SC_CODE"])
    school_code = str(school["SD_SCHUL_CODE"])

    st.success(
        f"선택한 학교: **{school_name}**"
    )

    address = school.get("ORG_RDNMA", "")

    if address and str(address) != "nan":
        st.caption(f"주소: {address}")


    # =====================================================
    # 날짜별 급식
    # =====================================================

    st.header("2. 날짜별 중식")

    today_kst = datetime.now(KST).date()

    selected_date = st.date_input(
        "급식 날짜",
        value=today_kst,
    )

    # 오늘 이후 날짜도 API가 제공할 수 있지만,
    # 달력 조회 자체는 선택한 날짜 그대로 허용한다.
    two_months_ago = selected_date - timedelta(days=62)

    with st.spinner("급식 정보를 불러오는 중..."):

        meal_df = get_meal_data(
            edu_code,
            school_code,
            two_months_ago,
            selected_date,
        )

    if meal_df.empty:

        st.info(
            "선택한 날짜의 급식 정보가 없습니다."
        )

    else:

        day_meal = get_selected_day_meal(
            meal_df,
            selected_date,
        )

        if day_meal.empty:

            st.info(
                f"{selected_date.strftime('%Y년 %m월 %d일')}에는 "
                "등록된 중식 정보가 없습니다."
            )

        else:

            row = day_meal.iloc[0]

            # -----------------------------
            # 메뉴
            # -----------------------------

            st.subheader(
                f"🍚 {selected_date.strftime('%Y년 %m월 %d일')} 중식"
            )

            menu_text = clean_menu_text(
                row.get("DDISH_NM", "")
            )

            menu_items = [
                item.strip()
                for item in menu_text.split("\n")
                if item.strip()
            ]

            if menu_items:

                for item in menu_items:
                    display_item = remove_allergen_numbers(
                        item
                    )
                    st.markdown(
                        f"- **{display_item}**"
                    )

            else:
                st.write("등록된 메뉴가 없습니다.")


            # -----------------------------
            # 알레르기
            # -----------------------------

            allergen_numbers = extract_allergen_numbers(
                menu_text
            )

            st.subheader("⚠️ 알레르기 정보")

            if allergen_numbers:

                allergen_text = ", ".join(
                    [
                        f"{number}. {ALLERGENS[number]}"
                        for number in allergen_numbers
                    ]
                )

                st.warning(allergen_text)

            else:

                st.write(
                    "메뉴에 표시된 알레르기 번호가 없습니다."
                )


            # -----------------------------
            # 칼로리
            # -----------------------------

            st.subheader("🔥 칼로리")

            cal_info = row.get(
                "CAL_INFO",
                "",
            )

            if pd.isna(cal_info):
                cal_info = ""

            if str(cal_info).strip():

                st.metric(
                    "중식 열량",
                    str(cal_info),
                )

            else:

                st.write(
                    "등록된 칼로리 정보가 없습니다."
                )


            # -----------------------------
            # 전체 영양정보
            # -----------------------------

            with st.expander("영양정보 자세히 보기"):

                ntr_info = row.get(
                    "NTR_INFO",
                    "",
                )

                if pd.isna(ntr_info):
                    ntr_info = ""

                st.text(
                    str(ntr_info)
                    if str(ntr_info).strip()
                    else "등록된 영양정보가 없습니다."
                )


    # =====================================================
    # 최근 두 달 단백질 분석
    # =====================================================

    st.header("3. 최근 두 달 단백질 분석")

    st.caption(
        "최근 약 2개월간 등록된 중식의 NTR_INFO에서 "
        "단백질(g)을 추출하여 요일별 평균을 계산합니다."
    )

    protein_df = prepare_protein_data(
        meal_df
    )

    if protein_df.empty:

        st.info(
            "단백질(g)을 파싱할 수 있는 최근 두 달간의 "
            "중식 영양정보가 없습니다."
        )

    else:

        # -----------------------------
        # 그래프
        # -----------------------------

        st.subheader(
            "요일별 평균 단백질"
        )

        chart_df = protein_df.set_index(
            "요일"
        )

        st.bar_chart(
            chart_df["단백질(g)"],
            height=400,
        )

        # -----------------------------
        # 표
        # -----------------------------

        display_df = protein_df.copy()

        display_df["단백질(g)"] = display_df[
            "단백질(g)"
        ].round(1)

        st.dataframe(
            display_df,
            hide_index=True,
            use_container_width=True,
        )

        # -----------------------------
        # 가장 높은 요일
        # -----------------------------

        max_row = protein_df.loc[
            protein_df["단백질(g)"].idxmax()
        ]

        max_day = str(
            max_row["요일"]
        )

        max_protein = float(
            max_row["단백질(g)"]
        )

        st.success(
            f"🥇 최근 두 달 기준으로 평균 단백질이 "
            f"가장 높은 요일은 **{max_day}**입니다. "
            f"평균 약 **{max_protein:.1f}g**입니다."
        )

        st.caption(
            "※ 학교에서 제공한 NTR_INFO에 단백질 값이 있는 "
            "중식 데이터만 계산에 포함됩니다."
        )


# =========================================================
# 하단 안내
# =========================================================

st.divider()

st.caption(
    "데이터 출처: 교육부·시도교육청 나이스(NEIS) 교육정보 개방 포털 Open API"
)

st.caption(
    "급식 데이터의 등록·수정 여부에 따라 실제 학교 급식표와 차이가 있을 수 있습니다."
)
