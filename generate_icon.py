# -*- coding: utf-8 -*-
"""초선명 크루즈 여객선(Ferry Ship) 전용 아이콘 생성기.
16x16부터 256x256까지 작은 해상도에서도 노란 폴더나 일반 창문으로 절대 오인되지 않고,
1초 만에 바다를 가르는 당당한 여객선(Ship)으로 인식되는 볼드(Bold) 마린 디자인입니다.
"""

from PIL import Image, ImageDraw

def create_distinct_ship_icon():
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 1. 상단 연돌(굴뚝) 연기 (Puffy White Smoke)
    smoke_fill = (245, 250, 255, 240)
    smoke_border = (200, 220, 240, 255)
    draw.ellipse([145, 26, 175, 54], fill=smoke_fill, outline=smoke_border, width=2)
    draw.ellipse([170, 16, 206, 48], fill=smoke_fill, outline=smoke_border, width=2)
    draw.ellipse([200, 10, 240, 44], fill=(245, 250, 255, 200), outline=smoke_border, width=2)

    # 2. 크루즈의 상징 굴뚝 (Iconic Red Funnel)
    # 우측으로 힘차게 기울어진 각도
    funnel = [
        (130, 56),
        (165, 56),
        (158, 104),
        (124, 104)
    ]
    draw.polygon(funnel, fill=(220, 20, 40, 255))
    # 굴뚝 상단 블랙 캡
    draw.polygon([(130, 56), (165, 56), (162, 68), (128, 68)], fill=(30, 35, 45, 255))
    # 굴뚝 중앙 화이트 밴드
    draw.polygon([(127, 78), (160, 78), (159, 88), (126, 88)], fill=(255, 255, 255, 255))

    # 3. 조타실 및 상부 선실 (Bridge & Top Cabin) - 순백색
    top_deck = [
        (75, 104),
        (185, 104),
        (185, 136),
        (65, 136)
    ]
    draw.polygon(top_deck, fill=(255, 255, 255, 255), outline=(170, 185, 205, 255), width=3)

    # 조타실 창문 (선명한 딥 마린 블루 사각 라운드)
    for wx in range(80, 175, 22):
        draw.rounded_rectangle([wx, 112, wx + 14, 126], radius=2, fill=(10, 50, 110, 255))

    # 4. 중간 객실 덱 (Mid Passenger Deck) - 화이트
    mid_deck = [
        (48, 136),
        (215, 136),
        (215, 168),
        (38, 168)
    ]
    draw.polygon(mid_deck, fill=(245, 248, 254, 255), outline=(170, 185, 205, 255), width=3)

    # 객실 창문: 시인성 극대화를 위한 대형 원형 포트홀 (Portholes) 5개
    for px in range(58, 205, 30):
        # 외곽 크롬 링
        draw.ellipse([px, 143, px + 18, 161], fill=(20, 90, 180, 255), outline=(140, 175, 215, 255), width=2)
        # 유리창 밝은 시안 하이라이트
        draw.ellipse([px + 3, px + 3 - (px - 143), px + 9, px + 9 - (px - 143)], fill=(120, 220, 255, 255))

    # 5. 웅장한 선체 (Mighty Ship Hull) - 진한 딥 네이비 블루
    # 뱃머리(좌측 선수)가 하늘로 높고 뾰족하게 치솟아 누가 봐도 100% 배 모양!
    hull = [
        (12, 168),   # 날렵하게 솟은 선수(Bow)
        (244, 168),  # 선미(Stern)
        (228, 210),
        (28, 210)
    ]
    draw.polygon(hull, fill=(12, 38, 85, 255), outline=(8, 24, 55, 255), width=3)

    # 선체 골드/화이트 띠 (럭셔리 크루즈 포인트 라인)
    draw.polygon([(18, 176), (241, 176), (239, 184), (20, 184)], fill=(255, 255, 255, 255))

    # 하단 선명한 레드 흘수선 (Vivid Red Keel)
    keel = [
        (28, 210),
        (228, 210),
        (208, 234),
        (56, 234)
    ]
    draw.polygon(keel, fill=(215, 35, 35, 255), outline=(160, 20, 20, 255), width=2)

    # 6. 바다 파도 (Deep Ocean Waves & Pure White Splash)
    # 짙은 군청색 파도
    wave_points_back = [
        (6, 224), (45, 220), (85, 226), (125, 218), (165, 224), (205, 218), (250, 224),
        (250, 252), (6, 252)
    ]
    draw.polygon(wave_points_back, fill=(10, 60, 130, 255))

    # 에메랄드 오션 앞 파도
    wave_points_front = [
        (6, 232), (40, 238), (80, 230), (120, 236), (160, 228), (200, 235), (250, 230),
        (250, 252), (6, 252)
    ]
    draw.polygon(wave_points_front, fill=(25, 125, 215, 255))

    # 뱃머리가 파도를 가르는 흰 물보라 (Dynamic Bow Wave)
    draw.polygon([(10, 214), (32, 228), (14, 235)], fill=(255, 255, 255, 255))
    draw.ellipse([4, 208, 22, 224], fill=(255, 255, 255, 255))
    draw.ellipse([18, 222, 34, 234], fill=(255, 255, 255, 255))

    # 파도 물방울 포말
    for wx in range(30, 245, 28):
        draw.ellipse([wx, 230, wx + 10, 236], fill=(255, 255, 255, 255))

    # PNG 저장
    img.save("ship_icon.png", "PNG")
    print("ship_icon.png 생성 완료")

    # 표준 7단계 멀티 해상도 ICO 생성
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save("ship_icon.ico", format="ICO", sizes=sizes)
    print("ship_icon.ico 멀티사이즈 생성 완료")

if __name__ == "__main__":
    create_distinct_ship_icon()
