import assert from 'node:assert/strict';
import test from 'node:test';
import { editorContent, characterPercent, setRatePercent, setCandidate, setUp,
  rateError, pityError, STANDARD_PROGRESSION } from '../pool-editor.mjs';

test('editing common controls preserves unrelated pool settings and item order', () => {
  const original = {
    probability_settings: {
      base_5star_rate: 0.008, base_4star_rate: 0.06, base_3star_rate: 0.932,
      _4star_weapon_rate: 0.25, custom_flag: 7,
    },
    probability_progression: {
      '4star': { hard_pity_pull: 10, hard_pity_rate: 1, soft_pity: [], custom: true },
      '5star': { ...STANDARD_PROGRESSION['5star'], custom: true },
    },
    included_item_ids: { '3star': ['w1'], '4star': ['c1', 'c2'], '5star': ['c3'] },
    rate_up_item_ids: { '3star': [], '4star': ['c1'], '5star': ['c3'] },
  };
  const content = editorContent(original);
  assert.equal(characterPercent(content), 75);
  setRatePercent(content, 'base_5star_rate', 1);
  setRatePercent(content, 'four_star_character_rate', 60);
  setCandidate(content, '4star', 'c2', false);
  setCandidate(content, '4star', 'c2', true);
  assert.equal(content.probability_settings.base_3star_rate, 0.93);
  assert.equal(content.probability_settings._4star_weapon_rate, 0.4);
  assert.equal(content.probability_settings.custom_flag, 7);
  assert.equal(content.probability_progression['5star'].custom, true);
  assert.deepEqual(content.included_item_ids['4star'], ['c1', 'c2']);
  assert.deepEqual(content.rate_up_item_ids['4star'], ['c1']);
  assert.equal(original.probability_settings.base_5star_rate, 0.008);
});

test('removing a candidate also removes its UP reference', () => {
  const content = editorContent({
    included_item_ids: { '5star': ['a', 'b'] },
    rate_up_item_ids: { '5star': ['a'] },
  });
  setCandidate(content, '5star', 'a', false);
  assert.deepEqual(content.included_item_ids['5star'], ['b']);
  assert.deepEqual(content.rate_up_item_ids['5star'], []);
  assert.throws(() => setUp(content, '5star', 'a', true), /请先/);
  setUp(content, '5star', 'b', true);
  assert.deepEqual(content.rate_up_item_ids['5star'], ['b']);
});

test('probability controls reject out-of-range values and invalid combined rates', () => {
  const content = editorContent();
  assert.throws(() => setRatePercent(content, 'base_5star_rate', 101), RangeError);
  setRatePercent(content, 'base_5star_rate', 60);
  setRatePercent(content, 'base_4star_rate', 50);
  assert.match(rateError(content), /100%/);
});

test('soft pity intervals must be ordered and end before hard pity', () => {
  const content = editorContent();
  assert.equal(pityError(content), '');
  content.probability_progression['5star'].soft_pity.push(
    { start_pull: 79, end_pull: 80, increment: 0.04 },
  );
  assert.match(pityError(content), /硬保底之前/);
  content.probability_progression['5star'].soft_pity = [
    { start_pull: 66, end_pull: 70, increment: 0.04 },
    { start_pull: 70, end_pull: 75, increment: 0.08 },
  ];
  assert.match(pityError(content), /不能重叠/);
});
