"""Pure, fail-closed selection rules. No network or purchase operations."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PureWindowsPath
import re
import unicodedata

GIB = 1024 ** 3
VIDEO = {'.mkv', '.mp4', '.avi', '.mov', '.wmv', '.ts', '.mpg', '.mpeg'}
SUBTITLE = {'.srt', '.smi', '.ass', '.ssa', '.vtt'}

# Original theatrical release year, not Korean re-release dates or upload dates.
# This is an explicit starter catalog, NOT every film released in the decade.
CATALOG_ROWS = [
    (1990, '사랑과 영혼', 'Ghost'),
    (1990, '가위손', 'Edward Scissorhands'),
    (1990, '귀여운 여인', 'Pretty Woman'),
    (1990, '나 홀로 집에', 'Home Alone'),
    (1990, '백 투 더 퓨쳐 3', 'Back to the Future Part III'),
    (1990, '다이 하드 2', 'Die Hard 2'),
    (1990, '토탈 리콜', 'Total Recall'),
    (1990, '붉은 10월', 'The Hunt for Red October'),
    (1990, '좋은 친구들', 'Goodfellas'),
    (1990, '장군의 아들', 'Son of a General'),
    (1991, '터미네이터 2', 'Terminator 2'),
    (1991, '양들의 침묵', 'The Silence of the Lambs'),
    (1991, '델마와 루이스', 'Thelma & Louise'),
    (1991, '포인트 브레이크', 'Point Break'),
    (1992, '나 홀로 집에 2', 'Home Alone 2'),
    (1992, '보디가드', 'The Bodyguard'),
    (1992, '원초적 본능', 'Basic Instinct'),
    (1992, '어 퓨 굿 맨', 'A Few Good Men'),
    (1992, '저수지의 개들', 'Reservoir Dogs'),
    (1992, '용서받지 못한 자', 'Unforgiven'),
    (1993, '쥬라기 공원', 'Jurassic Park'),
    (1993, '쉰들러 리스트', "Schindler's List"),
    (1993, '사랑의 블랙홀', 'Groundhog Day'),
    (1993, '도망자', 'The Fugitive'),
    (1993, '시애틀의 잠 못 이루는 밤', 'Sleepless in Seattle'),
    (1993, '서편제', 'Seopyeonje'),
    (1994, '쇼생크 탈출', 'The Shawshank Redemption'),
    (1994, '포레스트 검프', 'Forrest Gump'),
    (1994, '레옹', 'Leon The Professional'),
    (1994, '펄프 픽션', 'Pulp Fiction'),
    (1994, '스피드', 'Speed'),
    (1994, '중경삼림', 'Chungking Express'),
    (1995, '세븐', 'Se7en'),
    (1995, '유주얼 서스펙트', 'The Usual Suspects'),
    (1995, '브레이브하트', 'Braveheart'),
    (1995, '비포 선라이즈', 'Before Sunrise'),
    (1995, '다이 하드 3', 'Die Hard with a Vengeance'),
    (1995, '아폴로 13', 'Apollo 13'),
    (1996, '더 록', 'The Rock'),
    (1996, '미션 임파서블', 'Mission Impossible'),
    (1996, '인디펜던스 데이', 'Independence Day'),
    (1996, '파고', 'Fargo'),
    (1996, '제리 맥과이어', 'Jerry Maguire'),
    (1997, '타이타닉', 'Titanic'),
    (1997, '굿 윌 헌팅', 'Good Will Hunting'),
    (1997, '페이스 오프', 'Face Off'),
    (1997, '맨 인 블랙', 'Men in Black'),
    (1997, '접속', 'The Contact'),
    (1998, '트루먼 쇼', 'The Truman Show'),
    (1998, '라이언 일병 구하기', 'Saving Private Ryan'),
    (1998, '아마겟돈', 'Armageddon'),
    (1998, '8월의 크리스마스', 'Christmas in August'),
    (1998, '러시 아워', 'Rush Hour'),
    (1999, '매트릭스', 'The Matrix'),
    (1999, '식스 센스', 'The Sixth Sense'),
    (1999, '파이트 클럽', 'Fight Club'),
    (1999, '그린 마일', 'The Green Mile'),
    (1999, '노팅 힐', 'Notting Hill'),
    (1999, '미이라', 'The Mummy'),
    (1999, '쉬리', 'Shiri'),
]
CATALOG = [dict(id=f'{year}-{i:02}', year=year, title=ko, aliases=[ko, en])
           for i, (year, ko, en) in enumerate(CATALOG_ROWS)]


def compact(text: str) -> str:
    return re.sub(r'[^a-z0-9가-힣]', '', unicodedata.normalize('NFKC', text).lower())


def matches_movie(title: str, movie: dict) -> bool:
    """Require an exact release year token AND a catalog alias; never trust decade tags."""
    text = unicodedata.normalize('NFKC', title)
    years = {int(x) for x in re.findall(r'(?<!\d)((?:19|20)\d{2})(?!\d|\s*년대)', text)}
    if years != {movie['year']}:
        return False
    if re.search(r'(전편|합본|시리즈\s*모음|전집|컬렉션|collection|complete|시즌|s\d{2}e\d{2})', text, re.I):
        return False
    normalized = compact(text)
    for alias in movie['aliases']:
        key = compact(alias)
        at = normalized.find(key)
        if at < 0:
            continue
        tail = normalized[at + len(key):]
        # Do not confuse a sequel number with resolution or the release year.
        if tail and tail[0].isdigit() and not tail.startswith((str(movie['year']), '1080', '720', '2160', '480')):
            continue
        return True
    return False


def safe_relative(value: str, *, filename: bool = False) -> bool:
    if not isinstance(value, str) or not value or len(value) > 220:
        return False
    p = PureWindowsPath(value)
    if p.is_absolute() or p.drive or '..' in p.parts:
        return False
    if filename and len(p.parts) != 1:
        return False
    if any(re.search(r'[<>:"|?*\x00-\x1f]', part) or part.endswith((' ', '.'))
           or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I)
           for part in p.parts):
        return False
    return bool(p.parts)


def validate_destination(value: str) -> str:
    if not isinstance(value, str) or len(value) > 200:
        raise ValueError('저장 폴더 경로가 올바르지 않습니다.')
    p = PureWindowsPath(value.strip())
    if not p.is_absolute() or not re.fullmatch(r'[A-Za-z]:', p.drive):
        raise ValueError(r'E:\원본소스 같은 로컬 드라이브의 절대 경로를 입력하세요. 네트워크 경로는 지원하지 않습니다.')
    if len(p.parts) < 2 or '..' in p.parts or not safe_relative(str(PureWindowsPath(*p.parts[1:]))):
        raise ValueError('드라이브 루트나 특수 경로는 사용할 수 없습니다.')
    return str(p)


@dataclass
class Decision:
    allowed: bool
    reason: str
    files: list[dict]
    size: int = 0


def select_files(snapshot: dict, max_gib: int = 12) -> Decision:
    def no(message):
        return Decision(False, message, [])
    # Strict types prevent unexpected site schema changes from becoming permission.
    if snapshot.get('logged_in') is not True:
        return no('로그인되지 않았습니다.')
    if type(snapshot.get('fixed')) is not int or snapshot['fixed'] != 1:
        return no('현재 다운로드용 정액권 적용을 확인할 수 없습니다.')
    if snapshot.get('category') != 'F_02':
        return no('영화 분류가 아닙니다.')
    if type(snapshot.get('cine')) is not int or snapshot['cine'] != 0:
        return no('제휴 콘텐츠이거나 제휴 여부를 확인할 수 없습니다.')
    rows = snapshot.get('files')
    if not isinstance(rows, list) or not rows:
        return no('파일 목록을 읽을 수 없습니다.')
    selected = []
    for row in rows:
        if not isinstance(row, dict):
            return no('파일 목록 구조가 변경되었습니다.')
        name = row.get('name', '')
        if not safe_relative(name, filename=True):
            return no('안전하지 않거나 인식할 수 없는 파일 이름입니다.')
        folder = row.get('folder', '')
        if folder and not safe_relative(folder):
            return no('안전하지 않은 하위 폴더 경로입니다.')
        ext = PureWindowsPath(name).suffix.lower()
        if ext not in VIDEO | SUBTITLE:
            continue
        if row.get('cine') != '0' or row.get('disabled') is not False:
            return no('선택 파일에 제휴 파일 또는 다운로드 불가 파일이 있습니다.')
        if not isinstance(row.get('id'), str) or not re.fullmatch(r'\d+', row['id']):
            return no('파일 식별자를 확인할 수 없습니다.')
        size = row.get('size')
        if type(size) is not int or size <= 0:
            return no('파일 크기를 확인할 수 없습니다.')
        selected.append(row)
    videos = [f for f in selected if PureWindowsPath(f['name']).suffix.lower() in VIDEO]
    if len(videos) != 1:
        return no('단일 영화 파일 게시물만 처리합니다. 영상이 없거나 여러 개입니다.')
    size = sum(f['size'] for f in selected)
    if size > max_gib * GIB:
        return no(f'영화 한 편의 용량 제한({max_gib}GB)을 초과합니다.')
    if len({f['id'] for f in selected}) != len(selected):
        return no('파일 식별자가 중복되었습니다.')
    return Decision(True, '다운로드 정액권 적용 · 비제휴 · 단일 영상', selected, size)
