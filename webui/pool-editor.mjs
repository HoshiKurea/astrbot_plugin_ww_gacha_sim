export const RARITIES = ['5star', '4star', '3star'];

export const STANDARD_PROGRESSION = {
  '4star': { hard_pity_pull: 10, hard_pity_rate: 1, soft_pity: [] },
  '5star': {
    hard_pity_pull: 80, hard_pity_rate: 1,
    soft_pity: [
      { start_pull: 66, end_pull: 70, increment: 0.04 },
      { start_pull: 71, end_pull: 75, increment: 0.08 },
      { start_pull: 76, end_pull: 79, increment: 0.1 },
    ],
  },
};

const clone = (value) => JSON.parse(JSON.stringify(value));
const own = (object, key) => Object.prototype.hasOwnProperty.call(object, key);

export function editorContent(source = {}) {
  const content = clone(source);
  content.probability_settings ||= {};
  content.probability_progression ||= {};
  content.included_item_ids ||= {};
  content.rate_up_item_ids ||= {};
  const rates = content.probability_settings;
  rates.base_5star_rate ??= 0.008;
  rates.base_4star_rate ??= 0.06;
  rates.base_3star_rate ??= Number((1 - rates.base_5star_rate - rates.base_4star_rate).toFixed(6));
  rates.up_5star_rate ??= 0.5;
  rates.up_4star_rate ??= 0.5;
  if (!own(rates, 'four_star_character_rate') && !own(rates, '_4star_weapon_rate')) {
    rates.four_star_character_rate = own(rates, '_4star_role_rate') ? rates._4star_role_rate : 0.5;
  }
  for (const rarity of RARITIES) {
    content.included_item_ids[rarity] ||= [];
    content.rate_up_item_ids[rarity] ||= [];
  }
  for (const rarity of ['4star', '5star']) {
    content.probability_progression[rarity] ||= clone(STANDARD_PROGRESSION[rarity]);
    content.probability_progression[rarity].hard_pity_pull ??=
      STANDARD_PROGRESSION[rarity].hard_pity_pull;
    content.probability_progression[rarity].hard_pity_rate ??= 1;
    content.probability_progression[rarity].soft_pity ||= [];
  }
  return content;
}

export function percent(rate) {
  return Number((Number(rate) * 100).toFixed(4));
}

export function rateFromPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0 || number > 100) {
    throw new RangeError('百分比必须在 0 到 100 之间');
  }
  return Number((number / 100).toFixed(8));
}

export function characterPercent(content) {
  const rates = content.probability_settings;
  if (own(rates, '_4star_weapon_rate')) return percent(1 - rates._4star_weapon_rate);
  if (own(rates, 'four_star_character_rate')) return percent(rates.four_star_character_rate);
  return percent(rates._4star_role_rate ?? 0.5);
}

export function setRatePercent(content, key, value) {
  const rate = rateFromPercent(value);
  const rates = content.probability_settings;
  if (key === 'four_star_character_rate') {
    if (own(rates, '_4star_weapon_rate')) rates._4star_weapon_rate = Number((1 - rate).toFixed(8));
    rates.four_star_character_rate = rate;
    if (own(rates, '_4star_role_rate')) rates._4star_role_rate = rate;
  } else {
    rates[key] = rate;
    if (key === 'base_5star_rate' || key === 'base_4star_rate') {
      rates.base_3star_rate = Number((1 - rates.base_5star_rate - rates.base_4star_rate).toFixed(8));
    }
  }
}

export function setCandidate(content, rarity, id, included) {
  const selected = content.included_item_ids[rarity] ||= [];
  const up = content.rate_up_item_ids[rarity] ||= [];
  if (included && !selected.includes(id)) selected.push(id);
  if (!included) {
    content.included_item_ids[rarity] = selected.filter((value) => value !== id);
    content.rate_up_item_ids[rarity] = up.filter((value) => value !== id);
  }
}

export function setUp(content, rarity, id, enabled) {
  const selected = content.included_item_ids[rarity] ||= [];
  const up = content.rate_up_item_ids[rarity] ||= [];
  if (enabled) {
    if (!selected.includes(id)) throw new Error('请先将物品加入候选列表');
    if (!up.includes(id)) up.push(id);
  } else {
    content.rate_up_item_ids[rarity] = up.filter((value) => value !== id);
  }
}

export function rateError(content) {
  const rates = content.probability_settings;
  return Number(rates.base_5star_rate) + Number(rates.base_4star_rate) > 1 + 1e-9
    ? '五星和四星基础概率之和不能超过 100%。' : '';
}

export function pityError(content) {
  const progression = content.probability_progression;
  for (const rarity of ['4star', '5star']) {
    const block = progression[rarity];
    if (!Number.isInteger(block?.hard_pity_pull) || block.hard_pity_pull < 1) {
      return `${rarity[0]} 星硬保底次数必须是正整数。`;
    }
    if (Number(block.hard_pity_rate) !== 1) {
      return `${rarity[0]} 星硬保底概率必须为 100%。`;
    }
  }
  const intervals = progression['5star'].soft_pity;
  if (!Array.isArray(intervals)) return '五星软保底区间格式错误。';
  if (intervals.some((interval) => !interval || typeof interval !== 'object')) {
    return '五星软保底区间格式错误。';
  }
  let previousEnd = 0;
  for (const interval of [...intervals].sort((a, b) => a.start_pull - b.start_pull)) {
    if (!Number.isInteger(interval.start_pull) || !Number.isInteger(interval.end_pull)
      || interval.start_pull <= previousEnd || interval.end_pull < interval.start_pull
      || interval.end_pull >= progression['5star'].hard_pity_pull
      || !Number.isFinite(Number(interval.increment)) || Number(interval.increment) < 0
      || Number(interval.increment) > 1) {
      return '五星软保底区间不能重叠，且必须在硬保底之前。';
    }
    previousEnd = interval.end_pull;
  }
  return '';
}
