// Tests the real period maths from server.py, extracted verbatim.
// Date arithmetic is where off-by-one bugs live: month ends, leap years, ISO
// weeks spanning a year boundary, and Sunday (getDay() === 0).
//
//   node test_periods.js

const fmt = d => `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}`
               + `-${String(d.getDate()).padStart(2,'0')}`;

function periods(takenAt){
  const [y, mo, dd] = takenAt.slice(0,10).split('-').map(Number);
  const base = new Date(y, mo - 1, dd);
  const wkStart = new Date(base);
  wkStart.setDate(base.getDate() - ((base.getDay() + 6) % 7));   // Monday
  const wkEnd = new Date(wkStart); wkEnd.setDate(wkStart.getDate() + 6);
  return [
    ['Same day',   base,                    base],
    ['Same week',  wkStart,                 wkEnd],
    ['Same month', new Date(y, mo - 1, 1),  new Date(y, mo, 0)],
    ['Same year',  new Date(y, 0, 1),       new Date(y, 11, 31)],
  ].map(([label, s, e]) => ({label, since: fmt(s), until: fmt(e)}));
}

let fails = 0;
function eq(label, got, want){
  const ok = got === want;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}: ${got}${ok ? '' : ' (want ' + want + ')'}`);
  if(!ok) fails++;
}

function check(input, expected){
  const p = periods(input);
  console.log(`${input}`);
  const names = ['day', 'week', 'month', 'year'];
  p.forEach((x, i) => eq(names[i], `${x.since}..${x.until}`, expected[i]));
}

// a Tuesday mid-month
check('2025-09-30 17:16:24', [
  '2025-09-30..2025-09-30',
  '2025-09-29..2025-10-05',   // Mon 29 Sep – Sun 5 Oct
  '2025-09-01..2025-09-30',
  '2025-01-01..2025-12-31',
]);

// a Sunday: getDay() === 0 must map to the END of its week, not the start
check('2025-09-28 10:00:00', [
  '2025-09-28..2025-09-28',
  '2025-09-22..2025-09-28',
  '2025-09-01..2025-09-30',
  '2025-01-01..2025-12-31',
]);

// a Monday: week must start on itself
check('2025-09-29 10:00:00', [
  '2025-09-29..2025-09-29',
  '2025-09-29..2025-10-05',
  '2025-09-01..2025-09-30',
  '2025-01-01..2025-12-31',
]);

// week spanning a year boundary
check('2026-01-01 00:30:00', [
  '2026-01-01..2026-01-01',
  '2025-12-29..2026-01-04',
  '2026-01-01..2026-01-31',
  '2026-01-01..2026-12-31',
]);

// February in a leap year -> 29 days
check('2024-02-15 12:00:00', [
  '2024-02-15..2024-02-15',
  '2024-02-12..2024-02-18',
  '2024-02-01..2024-02-29',
  '2024-01-01..2024-12-31',
]);

// February in a non-leap year -> 28 days
check('2025-02-15 12:00:00', [
  '2025-02-15..2025-02-15',
  '2025-02-10..2025-02-16',
  '2025-02-01..2025-02-28',
  '2025-01-01..2025-12-31',
]);

// 31-day month end
check('2025-12-31 23:59:00', [
  '2025-12-31..2025-12-31',
  '2025-12-29..2026-01-04',
  '2025-12-01..2025-12-31',
  '2025-01-01..2025-12-31',
]);

console.log(fails ? `\n${fails} FAILED` : '\nALL PASS');
process.exit(fails ? 1 : 0);
