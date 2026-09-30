/*
 * Offline English -> Indic name transliteration (display only).
 *
 * Patient/contact names are always STORED exactly as typed (English letters).
 * When a regional UI language is active they are SHOWN in that script, e.g.
 * "Kingsley" -> किंग्सली (hi) / கிங்ஸ்லீ (ta). Nothing leaves the device.
 *
 * How it works:
 *   1. English spelling is tokenised phonetically (sh, ch, th, aa, ee, magic-e,
 *      final -y, n/m before a consonant = anusvara, ...) and written in
 *      Devanagari.
 *   2. The Indic Unicode blocks share one layout, so Devanagari is shifted into
 *      the target block, with per-script fixes (Tamil has no voiced/aspirated
 *      stops, Assamese ra/wa, Malayalam chillu letters, final virama in the
 *      southern scripts).
 * It is rule-based: good for Indian names, approximate for English spellings.
 */
(function () {
  'use strict';

  // ---------------------------------------------------------------- step 1: English -> Devanagari
  const VOWELS = [ // [spelling, independent, sign]
    ['aa', 'आ', 'ा'], ['ai', 'ऐ', 'ै'], ['au', 'औ', 'ौ'], ['ee', 'ई', 'ी'], ['ea', 'ई', 'ी'],
    ['ei', 'ई', 'ी'], ['ie', 'ई', 'ी'], ['oo', 'ऊ', 'ू'], ['ou', 'औ', 'ौ'], ['oa', 'ओ', 'ो'],
    ['a', 'अ', ''], ['e', 'ए', 'े'], ['i', 'इ', 'ि'], ['o', 'ओ', 'ो'], ['u', 'उ', 'ु']
  ];
  const LONG = { a: ['ए', 'े'], e: ['ई', 'ी'], i: ['आइ', 'ाइ'], o: ['ओ', 'ो'], u: ['ऊ', 'ू'] }; // magic-e
  const CONSONANTS = [
    ['ksh', 'क्ष'], ['chh', 'छ'], ['tch', 'च'], ['ch', 'च'], ['sh', 'श'], ['th', 'त'], ['dh', 'ध'],
    ['bh', 'भ'], ['ph', 'फ'], ['gh', 'घ'], ['kh', 'ख'], ['jh', 'झ'], ['ck', 'क'],
    ['b', 'ब'], ['d', 'द'], ['f', 'फ़'], ['g', 'ग'], ['h', 'ह'], ['j', 'ज'], ['k', 'क'], ['l', 'ल'],
    ['m', 'म'], ['n', 'न'], ['p', 'प'], ['q', 'क'], ['r', 'र'], ['s', 'स'], ['t', 'त'], ['v', 'व'],
    ['w', 'व'], ['x', 'क्स'], ['z', 'ज़']
  ];
  const VIRAMA = '्';
  const ANUSVARA = 'ं';
  const isVowelChar = (c) => 'aeiou'.includes(c);

  function tokenize(word) {
    const w = word.toLowerCase();
    const tokens = [];
    let i = 0;
    while (i < w.length) {
      const rest = w.slice(i);
      const c = w[i];
      // word-final "ey" (Kingsley, Shelley) -> long i
      if (rest === 'ey' && i > 0) { tokens.push({ t: 'V', ind: 'ई', sign: 'ी' }); break; }
      if (c === 'y') {
        const prev = w[i - 1], next = w[i + 1];
        const consonantY = i === 0 || (next && isVowelChar(next)) || (!next && prev && isVowelChar(prev));
        if (consonantY) tokens.push({ t: 'C', dev: 'य', raw: 'y' });
        else tokens.push({ t: 'V', ind: 'ई', sign: 'ी' });
        i += 1; continue;
      }
      if (c === 'c') { // soft c before e/i/y
        const soft = 'eiy'.includes(w[i + 1] || '') && w[i + 1] !== undefined && !rest.startsWith('ch') && !rest.startsWith('ck');
        if (soft) { tokens.push({ t: 'C', dev: 'स', raw: 'c' }); i += 1; continue; }
      }
      const v = VOWELS.find(([s]) => rest.startsWith(s));
      if (v) { tokens.push({ t: 'V', key: v[0], ind: v[1], sign: v[2] }); i += v[0].length; continue; }
      // "th" is dental in Indian names (Geetha, Thilak) but aspirated word-finally (Siddharth)
      if (rest === 'th' && i > 0) { tokens.push({ t: 'C', dev: 'थ', raw: 'th' }); i += 2; continue; }
      const k = CONSONANTS.find(([s]) => rest.startsWith(s));
      if (k) { tokens.push({ t: 'C', dev: k[1], raw: k[0] }); i += k[0].length; continue; }
      i += 1; // unknown character: skip
    }
    return tokens;
  }

  function wordToDevanagari(word, finalVirama) {
    const tokens = tokenize(word);
    const n = tokens.length;
    if (!n) return word;

    // Silent magic-e: vowel + one consonant + final "e" (Jude, Mike, Kate)
    const last = tokens[n - 1];
    if (n >= 3 && last.t === 'V' && last.key === 'e' && tokens[n - 2].t === 'C' && tokens[n - 3].t === 'V'
        && LONG[tokens[n - 3].key]) {
      const [ind, sign] = LONG[tokens[n - 3].key];
      tokens[n - 3] = { t: 'V', ind, sign };
      tokens.pop();
    } else if (n >= 2 && last.t === 'V' && last.key === 'e' && tokens[n - 2].t === 'C'
        && tokens.slice(0, -2).some((t) => t.t === 'V')) {
      tokens.pop(); // silent final e after a consonant cluster (Vance, Grace)
    }

    let out = '';
    let afterConsonant = false;
    tokens.forEach((tok, idx) => {
      const isLast = idx === tokens.length - 1;
      if (tok.t === 'V') {
        if (afterConsonant) {
          // word-final short a after a consonant is heard long (Priya, Rama)
          const finalAr = tok.key === 'a' && idx === tokens.length - 2 && tokens[idx + 1].raw === 'r';
          out += (tok.key === 'a' && (isLast || finalAr)) ? 'ा' : tok.sign;
        } else {
          out += tok.ind;
        }
        afterConsonant = false;
        return;
      }
      const next = tokens[idx + 1];
      const prev = tokens[idx - 1];
      const nasal = (tok.raw === 'n' || tok.raw === 'm') && prev && prev.t === 'V' && next && next.t === 'C'
        && !['n', 'm', 'y', 'r', 'h', 'v', 'w'].includes(next.raw);
      if (nasal) { out += ANUSVARA; afterConsonant = false; return; }
      if (afterConsonant) out += VIRAMA;
      out += tok.dev;
      afterConsonant = true;
    });
    if (afterConsonant && finalVirama) out += VIRAMA;
    return out;
  }

  // ---------------------------------------------------------------- step 2: Devanagari -> target script
  const BASE = { hi: 0x0900, as: 0x0980, ta: 0x0B80, te: 0x0C00, kn: 0x0C80, ml: 0x0D00 };
  const FINAL_VIRAMA = { ta: true, te: true, kn: true, ml: true };

  // Tamil lacks voiced/aspirated stops: fold onto the base letter of each group.
  const TAMIL_FOLD = {
    'ख': 'क', 'ग': 'क', 'घ': 'क', 'छ': 'च', 'झ': 'ज', 'ठ': 'ट', 'ड': 'ट', 'ढ': 'ट',
    'थ': 'त', 'द': 'त', 'ध': 'त', 'फ': 'प', 'ब': 'प', 'भ': 'प', 'श': 'ष'
  };

  function shift(dev, lang) {
    const base = BASE[lang];
    // magic-e "i" (Mike): Hindi writes माइक, the other scripts use the ai sign
    const chars = [...dev.replace(/ाइ/g, 'ै')];
    let out = '';
    chars.forEach((ch, i) => {
      const cp = ch.codePointAt(0);
      if (cp < 0x0900 || cp > 0x097F) { out += ch; return; }
      let c = ch;
      if (c === '़') return; // nukta only matters in Hindi
      if (lang === 'as') {
        if (c === 'र') { out += 'ৰ'; return; }
        if (c === 'व') { out += 'ৱ'; return; }
        if (c === 'य') { out += chars[i - 1] === VIRAMA ? 'য' : 'য়'; return; } // য় = the y sound; য after virama = ya-phala
      }
      if (lang === 'ta') {
        if (c === 'फ' && chars[i + 1] === '़') { out += 'ஃப'; return; } // f is written ஃப
        c = TAMIL_FOLD[c] || c;
      }
      out += String.fromCodePoint(base + (c.codePointAt(0) - 0x0900));
    });
    return out;
  }

  function fixTamil(word) {
    let w = word;
    // dental ந only word-initially; alveolar ன elsewhere
    w = w.replace(/(?!^)ந/g, 'ன');
    // anusvara -> the nasal of the following consonant's class
    w = w.replace(/ஂ(?=க)/g, 'ங்').replace(/ஂ(?=[சஜ])/g, 'ஞ்').replace(/ஂ(?=ட)/g, 'ண்')
         .replace(/ஂ(?=த)/g, 'ந்').replace(/ஂ(?=[பமவ])/g, 'ம்').replace(/ஂ/g, 'ன்');
    return w;
  }

  function fixMalayalam(word) {
    // anusvara before a consonant -> that consonant's nasal (ആനന്ദ്, സഞ്ജയ്)
    word = word.replace(/ം(?=[കഖഗഘ])/g, 'ങ്').replace(/ം(?=[ചഛജഝ])/g, 'ഞ്').replace(/ം(?=[ടഠഡഢ])/g, 'ണ്')
               .replace(/ം(?=[തഥദധ])/g, 'ന്').replace(/ം(?=[പഫബഭ])/g, 'മ്');
    // chillu letters for a word-final dead n / l / r / ll / nn
    return word
      .replace(/ന്$/, 'ൻ').replace(/ല്$/, 'ൽ').replace(/ര്$/, 'ർ')
      .replace(/ള്$/, 'ൾ').replace(/ണ്$/, 'ൺ');
  }

  // ---------------------------------------------------------------- public API
  function transliterate(text, lang) {
    if (!text || !BASE[lang]) return text;
    return String(text).replace(/[A-Za-z]+/g, (word) => {
      const dev = wordToDevanagari(word, !!FINAL_VIRAMA[lang]);
      if (lang === 'hi') return dev;
      let out = shift(dev, lang);
      if (lang === 'ta') out = fixTamil(out);
      if (lang === 'ml') out = fixMalayalam(out);
      return out;
    });
  }

  window.LifeLinkTranslit = { transliterate };
  if (typeof module !== 'undefined') module.exports = { transliterate };
})();
