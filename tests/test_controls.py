"""Regression cases captured from the actual pinned Diamond/core UI.

In particular, forced-party memory input becomes true before the black/fading
bottom screen has drawn. These images must not cause a click.
"""
from pathlib import Path
import unittest

from PIL import Image, ImageEnhance

from cynthiabench.controls import party_touch_position, party_ui_kind, wait_for_party_ui

FIXTURES = Path(__file__).parent / 'fixtures' / 'controls'


def screenshot(name):
    if not (FIXTURES / (name + '.png')).exists():
        raise unittest.SkipTest('Private game screenshot fixture is not distributed')
    with Image.open(FIXTURES / (name + '.png')) as image:
        return image.convert('RGB')


class PartyReadinessTests(unittest.TestCase):
    def test_party_mapping_preserves_original_identity_after_switches(self):
        initial = [0, 1, 2, 3, 4, 5]
        self.assertEqual(party_touch_position(1, initial), (64, 26))
        self.assertEqual(party_touch_position(6, initial), (192, 130))
        # Actual screen after switching to original slot2: Staraptor top-left,
        # injured/fainted original slot1 Infernape top-right.
        after_staraptor = [1, 0, 2, 3, 4, 5]
        self.assertEqual(party_touch_position(1, after_staraptor), (192, 34))
        self.assertEqual(party_touch_position(2, after_staraptor), (64, 26))
        # Repeated swaps are not equivalent to sorting active first.
        after_luxray = [2, 0, 1, 3, 4, 5]
        self.assertEqual(party_touch_position(2, after_luxray), (64, 74))
        self.assertEqual(party_touch_position(3, after_luxray), (64, 26))

    def test_party_mapping_rejects_invalid_or_incomplete_maps(self):
        for mapping in (None, [0, 1, 2], [0, 0, 2, 3, 4, 5], [False, 1, 2, 3, 4, 5]):
            with self.assertRaises(ValueError):
                party_touch_position(1, mapping)
        with self.assertRaises(ValueError):
            party_touch_position(0, list(range(6)))

    def test_real_normal_forced_and_medicine_grids(self):
        for name in ('normal-grid', 'forced-grid', 'medicine-grid'):
            with self.subTest(name=name):
                self.assertEqual(party_ui_kind(screenshot(name)), 'party_grid')

    def test_real_shift_with_different_species(self):
        for name in ('normal-shift', 'forced-shift'):
            with self.subTest(name=name):
                self.assertEqual(party_ui_kind(screenshot(name)), 'shift')

    def test_real_free_shift_offer_is_distinct_and_requires_label(self):
        offer=screenshot('shift-offer')
        self.assertEqual(party_ui_kind(offer),'shift_offer')
        offer.paste((40,81,113),(86,329,124,337))
        self.assertEqual(party_ui_kind(offer),'unknown')

    def test_learning_dialogs_are_distinct_from_free_switch_choices(self):
        for name,kind in [('learn-offer','learn_offer'),('learn-slots','learn_slots'),
                          ('learn-confirm-stop','learn_confirm_stop'),('learn-forget','learn_forget')]:
            with self.subTest(name=name):
                self.assertEqual(party_ui_kind(screenshot(name)),kind)
        offer=screenshot('learn-offer');offer.paste((154,81,81),(80,257,128,265))
        self.assertEqual(party_ui_kind(offer),'unknown')

    def test_black_fade_animation_and_other_menus_are_not_ready(self):
        for name in ('forced-black', 'forced-fade', 'switch-animation',
                     'medicine-detail', 'command'):
            with self.subTest(name=name):
                self.assertEqual(party_ui_kind(screenshot(name)), 'unknown')

    def test_scaled_or_dimmed_ui_fails_closed(self):
        for name in ('normal-grid','forced-grid','normal-shift','shift-offer',
                     'learn-offer','learn-slots','learn-confirm-stop','learn-forget'):
            image = screenshot(name)
            self.assertEqual(party_ui_kind(image.resize((512, 768))), 'unknown')
            self.assertEqual(party_ui_kind(ImageEnhance.Brightness(image).enhance(.85)), 'unknown')

    def test_incomplete_grid_and_green_card_without_shift_are_not_ready(self):
        grid = screenshot('normal-grid')
        grid.paste((0, 0, 0), (0, 290, 256, 344))
        self.assertEqual(party_ui_kind(grid), 'unknown')
        shift = screenshot('normal-shift')
        shift.paste((56, 154, 81), (108, 299, 148, 307))
        self.assertEqual(party_ui_kind(shift), 'unknown')

    def test_wait_requires_consecutive_ready_frames_and_never_presses(self):
        frames = [screenshot(name) for name in (
            'forced-black', 'forced-fade', 'forced-grid', 'forced-black',
            'forced-grid', 'forced-grid', 'forced-grid')]
        class Emulator:
            halted = False
            index = 0
            def image(self):
                return frames[min(self.index, len(frames)-1)]
            def step(self, count):
                self.index += count
        emulator = Emulator()
        self.assertEqual(wait_for_party_ui(emulator, 'party_grid', max_frames=10), 6)
        self.assertEqual(emulator.index, 6)

    def test_wait_times_out_or_halts_without_clicking(self):
        class Emulator:
            halted = False
            frames = 0
            def image(self):
                return screenshot('forced-black')
            def step(self, count):
                self.frames += count
        emulator = Emulator()
        with self.assertRaises(TimeoutError):
            wait_for_party_ui(emulator, 'party_grid', max_frames=5)
        self.assertEqual(emulator.frames, 5)
        emulator.halted = True
        with self.assertRaises(RuntimeError):
            wait_for_party_ui(emulator, 'shift')


if __name__ == '__main__':
    unittest.main()
