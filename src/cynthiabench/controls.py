"""UI helpers that execute a choice; these never choose a battle strategy."""
from __future__ import annotations


def party_touch_position(slot, party_order):
    """Map a public one-based party slot to its current lower-screen card.

    BattleContext+0x312C contains display-position -> original-party-index.
    Party and bag emitters both copy that permutation. Normal singles switches
    swap the incoming Pokemon to display position zero; they do not reorder the
    underlying saved Party. Only the host's validated BattleMemory map is valid.
    """
    if type(slot) is not int or not 1 <= slot <= 6:
        raise ValueError('Party slot must be 1..6')
    if (not isinstance(party_order, (list, tuple)) or len(party_order) != 6
            or any(type(index) is not int for index in party_order)
            or sorted(party_order) != list(range(6))):
        raise ValueError('Party display order must be a permutation of 0..5')
    position = party_order.index(slot - 1)
    return (64 if position % 2 == 0 else 192,
            26 + (position // 2) * 48 + (8 if position % 2 else 0))


def party_ui_kind(image):
    """Identify fully drawn native Diamond party/SHIFT screens, or ``unknown``.

    This is a rendering guard, not a game-state oracle. Callers must additionally
    validate the appropriate input state in BattleMemory and verify the selected
    party identity after a switch. Exact native RGB landmarks deliberately reject
    black screens, fades, scaled images and partially drawn menus. They avoid
    names, sprites, HP, gender and living/fainted card colours.
    """
    if image.size != (256, 384) or image.mode not in ('RGB', 'RGBA'):
        return 'unknown'
    def matches(points, colour):
        return all(image.getpixel(point)[:3] == colour for point in points)
    def glyph(x,y,width,height,colour):
        return tuple(''.join('#' if image.getpixel((xx,yy))[:3]==colour else '.'
                             for xx in range(x,x+width)) for yy in range(y,y+height))

    # The move-learning prompt shares button geometry with Shift, but must not
    # share its label guard. These fixed text fragments contain no Pokemon/move
    # name and therefore also work for other legitimate learning offers.
    choice_buttons=(matches([(8,240),(247,240),(8,280)],(235,56,56))
                    and matches([(60,250),(192,250)],(154,81,81))
                    and matches([(60,320),(192,320)],(40,81,113))
                    and matches([(8,306),(247,306)],(186,235,251)))
    if choice_buttons:
        if glyph(80,257,48,8,(251,251,251))==(
            '...#####........................................',
            '...#............................................',
            '...#..................................##........',
            '...#......####....###...#####..####...####......',
            '...####...#..##..#..##..#..##..#..##...#........',
            '...#......#..##..#......#..##..#####...#........',
            '...#......#..##..#.......####..#.......#........',
            '...#.......###...#.........##...###.....##......',
        ):return 'learn_offer'
        if glyph(16,155,44,8,(81,81,89))==(
            '.###..#.................##......#...........',
            '#...#.#..................#......#...........',
            '#.....#..................#......#......#....',
            '#.....####...###..#...#..#...####.....####..',
            '.###..#...#.#...#.#...#..#..#...#......#....',
            '....#.#...#.#...#.#...#..#..#...#......#....',
            '....#.#...#.#...#.#...#..#..#...#......#....',
            '#...#.#...#.#...#.#...#..#..#...#......#....',
        ):return 'learn_confirm_stop'
    if (matches([(2,246),(126,246),(130,246),(253,246),(4,242),(4,290)],(73,73,73))
            and matches([(8,250),(8,298),(248,250),(248,298)],(56,154,81))
            and matches([(12,352),(200,352)],(227,178,56))
            and matches([(16,374),(200,374)],(251,211,89))
            and matches([(128,234),(80,202)],(154,97,227))):
        return 'learn_slots'
    if (matches([(4,350),(200,350)],(251,211,211))
            and matches([(60,362),(150,362)],(211,89,89))
            and matches([(16,244),(200,244)],(195,138,32))
            and glyph(76,363,56,7,(251,251,251))==(
                '.........#####..####...####...####...#####..#####.......',
                '.........#......#..##..#..##..#..##..#........#.........',
                '.........#......#...#..#..##..#......#........#.........',
                '.........#......#...#..#..##..#......####.....#.........',
                '.........####...#...#..####...#..##..#........#.........',
                '.........#......#...#..#...#..#..##..#........#.........',
                '.........#......#...#..#...#..#..##..#........#.........',
            )):return 'learn_forget'

    # Shift-style free-switch offer, not a normal party-card SHIFT action.
    # Require both fully drawn buttons and the literal "Keep" glyph fragment.
    if (matches([(8,240),(247,240),(8,280)],(235,56,56))
            and matches([(60,250),(192,250)],(154,81,81))
            and matches([(60,320),(192,320)],(40,81,113))
            and matches([(8,306),(247,306)],(186,235,251))):
        expected=(
            '..#..#.............................#..',
            '..#.##.............................#..',
            '..###..............................#..',
            '..###....####...####...####........###',
            '..###....#..##..#..##..#..##.......#..',
            '..#..#...#####..#####..#..##.......#..',
            '..#..#...#......#......#..##.......#..',
            '..#...#...###....###...####........###',
        )
        observed=tuple(''.join('#' if image.getpixel((x,y))[:3]==(251,251,251)
                               else '.' for x in range(86,124))
                       for y in range(329,337))
        if observed==expected:return 'shift_offer'

    # Six staggered card outlines, purple central gutter, and prompt frame.
    card_edges = [(x, y) for x in (126, 254) for y in (220, 268, 316)]
    card_tops = [(100, 194), (228, 202), (100, 242), (228, 250),
                 (100, 290), (228, 298)]
    if (matches(card_edges + card_tops, (73, 73, 73))
            and matches([(128, 220), (128, 260), (128, 310)], (130, 89, 195))
            and matches([(16, 354), (200, 354), (16, 380), (200, 380)], (81, 89, 89))):
        return 'party_grid'

    # Large green action card and separate blue SUMMARY / CHECK MOVES buttons.
    # The literal SHIFT glyph mask prevents treating another green card as SHIFT.
    if (matches([(60, 212), (192, 212), (60, 320), (192, 320)], (56, 154, 81))
            and matches([(60, 350), (160, 350)], (162, 243, 251))
            and matches([(16, 354), (200, 354)], (24, 105, 178))):
        expected = (
            '....####...#...#...###...#####..#####...',
            '....#..##..#...#....#....#........#.....',
            '....#......#...#....#....#........#.....',
            '.....##....#...#....#....#........#.....',
            '.......#...#####....#....####.....#.....',
            '.......##..#...#....#....#........#.....',
            '....#..##..#...#...##....#........#.....',
            '.....###...#...#...###...#........#.....',
        )
        observed = tuple(''.join('#' if image.getpixel((x, y))[:3] == (251, 251, 251)
                                 else '.' for x in range(108, 148))
                         for y in range(299, 307))
        if observed == expected:
            return 'shift'
    return 'unknown'


def wait_for_party_ui(emulator, kind, max_frames=600, stable_frames=3):
    """Advance neutral frames until the requested screen is visibly stable.

    Returns emulated frames elapsed. Fails closed instead of clicking an unknown
    screen. This helper makes no strategic choice and never infers an outcome.
    """
    if kind not in ('party_grid','shift','shift_offer','learn_offer','learn_slots','learn_confirm_stop','learn_forget'):
        raise ValueError('Unknown UI readiness kind')
    if max_frames < 0 or stable_frames < 1:
        raise ValueError('Invalid readiness limits')
    consecutive = 0
    for elapsed in range(max_frames + 1):
        if getattr(emulator, 'halted', False):
            raise RuntimeError('Emulator halted before party UI became ready')
        if party_ui_kind(emulator.image()) == kind:
            consecutive += 1
            if consecutive >= stable_frames:
                return elapsed
        else:
            consecutive = 0
        if elapsed < max_frames:
            emulator.step(1)
    raise TimeoutError(f'{kind} was not visibly ready within {max_frames} frames')


def ui_kind(image):
    """Conservative visual fallback. A positive match only describes a menu.

    Game outcome is never inferred here. Use battle_memory for scoring.
    """
    if image.size != (256, 384):
        return 'unknown'
    def rgb(x,y): return image.getpixel((x,y))[:3]
    r,g,b = rgb(128,270)
    pr,pg,pb = rgb(235,350)
    br,bg,bb = rgb(125,360)
    if r>g*1.45 and r>b*1.2 and pg>pr*1.3 and bb>br*1.3:
        return 'command'
    # Move slots each have a coloured type background; cancel spans the bottom.
    cr,cg,cb = rgb(128,370)
    left=image.getpixel((6,220))[:3]
    if cb>cr*1.4 and cg>cr and max(left)-min(left)>20:
        # Distinguish from main and party screens through the four button borders.
        if image.getpixel((127,230))[:3] == image.getpixel((127,305))[:3]:
            return 'moves_candidate'
    return 'unknown'


def touch(emulator, x, y, after=20):
    emulator.step(3, touch=(x,y))
    emulator.step(after)


def choose_move(emulator, slot):
    if not 1 <= slot <= 4:
        raise ValueError('Move slot must be 1..4')
    if ui_kind(emulator.image()) == 'command':
        touch(emulator,128,80,after=25)
    x,y = [(64,48),(192,48),(64,114),(192,114)][slot-1]
    touch(emulator,x,y,after=1)


def advance_until_command(emulator, max_frames=2400):
    """Let animations run without any button presses or choices."""
    for i in range(max_frames):
        emulator.step(1)
        if i>20 and i%5==0 and ui_kind(emulator.image()) == 'command':
            return i+1
    return max_frames
