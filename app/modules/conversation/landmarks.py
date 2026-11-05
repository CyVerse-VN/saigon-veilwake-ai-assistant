"""Public place identifiers shared by mobile context and conversational topic matching."""

from __future__ import annotations

import re
import unicodedata

LANDMARK_NAMES = {
    "post_office": "Bưu điện Trung tâm Sài Gòn",
    "cathedral": "Nhà thờ Đức Bà Sài Gòn",
    "independence_palace": "Dinh Độc Lập",
    "ben_thanh": "Chợ Bến Thành",
    "war_remnants": "Bảo tàng Chứng tích Chiến tranh",
    "city_hall": "Ủy ban nhân dân Thành phố Hồ Chí Minh",
    "opera_house": "Nhà hát Thành phố Hồ Chí Minh",
    "bitexco": "Tháp tài chính Bitexco",
    "landmark_81": "Landmark 81",
    "tan_dinh_church": "Nhà thờ Tân Định",
    "nha_rong_wharf": "Bến Nhà Rồng",
    "jade_emperor": "Chùa Ngọc Hoàng",
    "thien_hau": "Chùa Bà Thiên Hậu",
    "turtle_lake": "Hồ Con Rùa",
    "binh_tay": "Chợ Bình Tây",
    "fine_arts_museum": "Bảo tàng Mỹ thuật Thành phố Hồ Chí Minh",
    "city_museum": "Bảo tàng Thành phố Hồ Chí Minh",
    "vinh_nghiem": "Chùa Vĩnh Nghiêm",
    "history_museum": "Bảo tàng Lịch sử Thành phố Hồ Chí Minh",
    "mong_bridge": "Cầu Mống",
    "saigon_zoo": "Thảo Cầm Viên Sài Gòn",
    "nguyen_hue": "Phố đi bộ Nguyễn Huệ",
    "bach_dang": "Bến Bạch Đằng",
    "book_street": "Đường sách Nguyễn Văn Bình",
}

_ALIASES = {
    "nhà thờ đức bà": "cathedral",
    "nhà thờ chính tòa đức bà sài gòn": "cathedral",
    "bưu điện trung tâm": "post_office",
    "hội trường thống nhất": "independence_palace",
    "tòa thị chính": "city_hall",
    "trụ sở ubnd": "city_hall",
    "ủy ban nhân dân": "city_hall",
    "nhà hát thành phố": "opera_house",
    "bitexco": "bitexco",
    "bảo tàng mỹ thuật": "fine_arts_museum",
    "bảo tàng lịch sử": "history_museum",
    "dinh gia long": "city_museum",
    "thảo cầm viên": "saigon_zoo",
    "cột cờ thủ ngữ": "bach_dang",
    "đường sách": "book_street",
}


def fold_vietnamese(text: str) -> str:
    """Accent-insensitive matching; never replace the original text used as evidence."""
    decomposed = unicodedata.normalize("NFD", text.casefold().replace("đ", "d"))
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def known_landmark_topics(query: str) -> tuple[str, ...]:
    folded = fold_vietnamese(query)
    # A rejected alternative is not the subject to retrieve ("X, không phải Y").
    positive = re.split(r"\b(?:chu khong phai|khong phai|thay vi)\b", folded)[0]
    if positive.strip():
        folded = positive
    names = {**{name: key for key, name in LANDMARK_NAMES.items()}, **_ALIASES}
    matches = []
    for name, key in names.items():
        match = re.search(r"\b" + re.escape(fold_vietnamese(name)) + r"\b", folded)
        if match:
            matches.append((match.start(), -len(name), LANDMARK_NAMES[key]))
    return tuple(dict.fromkeys(topic for _, _, topic in sorted(matches)))


def known_landmark_topic(query: str) -> str | None:
    return next(iter(known_landmark_topics(query)), None)


_PLACE_QUESTION = re.compile(
    r"^(?:(?:cho (?:toi|minh) hoi|xin hoi|con|va)\s+)?(?:"
    r"gioi thieu them|ke them|chi tiet hon|"
    r"dia chi(?: la gi| o dau)?|o dau(?: vay)?|nam o dau|"
    r"(?:duoc )?xay(?: dung)?(?: (?:vao|tu))? (?:nam nao|khi nao)|"
    r"bao nhieu tuoi|ai (?:thiet ke|xay dung)|"
    r"(?:co )?(?:phong cach )?kien truc(?: gi| nhu the nao| the nao)?|"
    r"lich su(?: ra sao| nhu the nao| the nao)?|co gi (?:hay|thu vi|dac biet)|"
    r"(?:mo|dong) cua(?: (?:luc|may) gio| khi nao)?|may gio (?:mo|dong) cua|"
    r"gio (?:mo|dong) cua(?: la gi)?|gia ve(?: bao nhieu| the nao)?|"
    r"ve (?:bao nhieu|co dat khong)|co mat phi khong|"
    r"nen (?:tham quan|di) luc nao|tham quan mat bao lau"
    r")(?: (?:vay|nhe|a))?[?.!\s]*$"
)

def is_subject_free_place_question(query: str) -> bool:
    return bool(_PLACE_QUESTION.fullmatch(fold_vietnamese(query.strip())))
