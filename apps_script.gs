/**
 * Хранилище реестра в Google-таблице.
 *
 * Роль: таблица — единственная правда. Бот пишет сюда, читает отсюда, а ты
 * открываешь её с телефона и видишь цифры без всякого бота. Версии и бэкапы
 * даёт Google.
 *
 *   GET  ?secret=...          -> { ok, ledger }   текущий реестр (JSON)
 *   POST { secret, ledger }   -> { ok }           сохранить и перерисовать листы
 *
 * Канонический JSON лежит в скрытом листе «_data», ячейка A1 (лимит 50 000
 * символов — с запасом; в Script Properties влезло бы только 9 КБ).
 * Листы «Реестр» и «Расходы» перерисовываются из него — они для глаз,
 * править их руками бессмысленно: бот перезапишет.
 *
 * Установка:
 *   1. Создай таблицу -> Расширения -> Apps Script, вставь этот файл.
 *   2. Настройки проекта -> Свойства скрипта: SHEET_SECRET = длинная строка.
 *   3. Развернуть -> Веб-приложение, «Запуск от имени: Я», «Доступ: у кого есть ссылка».
 *   4. URL и секрет пропиши в переменные Railway: SHEET_WEBHOOK_URL, SHEET_SECRET.
 */

var DATA_SHEET = '_data';
var LEDGER_SHEET = 'Реестр';
var EXPENSES_SHEET = 'Расходы';
var JOURNAL_SHEET = 'Журнал';

function _secret() {
  return PropertiesService.getScriptProperties().getProperty('SHEET_SECRET') || '';
}

/**
 * Fail-closed: секрет не задан — не пускаем НИКОГО.
 * Без этой проверки незаданный секрет означал бы _secret() === '' и сравнение
 * '' !== '' === false, то есть запрос БЕЗ секрета проходил бы. Веб-приложение
 * развёрнуто с доступом «Все», так что это открыло бы реестр всему интернету.
 */
function _denied(e) {
  var secret = _secret();
  if (!secret) {
    return _json({ ok: false, error: 'SHEET_SECRET не задан в свойствах скрипта' });
  }
  if ((e || '') !== secret) {
    return _json({ ok: false, error: 'forbidden' });
  }
  return null;
}

function _json(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function _sheet(name, hidden) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(name);
  if (!sh) {
    sh = ss.insertSheet(name);
    if (hidden) sh.hideSheet();
  }
  return sh;
}

function _num(x) {
  var n = Number(x);
  return isFinite(n) ? n : 0;
}

function _fmt(n) {
  // 1089030 -> "1 089 030"
  return String(Math.round(_num(n))).replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
}

// ---------- Чтение ----------
function doGet(e) {
  var denied = _denied(e.parameter.secret);
  if (denied) return denied;
  if (e.parameter.log) {
    return _json({ ok: true, log: readJournal(_num(e.parameter.log) || 15) });
  }
  var raw = _sheet(DATA_SHEET, true).getRange('A1').getValue();
  var ledger = null;
  if (raw) {
    try { ledger = JSON.parse(raw); } catch (err) { ledger = null; }
  }
  return _json({ ok: true, ledger: ledger });
}

// ---------- Запись ----------
function doPost(e) {
  var body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return _json({ ok: false, error: 'bad json' });
  }
  var denied = _denied(body.secret);
  if (denied) return denied;
  if (!body.ledger) {
    return _json({ ok: false, error: 'no ledger' });
  }

  // Блокировка от гонки: бот может писать из нескольких запросов подряд.
  var lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    _sheet(DATA_SHEET, true).getRange('A1').setValue(JSON.stringify(body.ledger));
    renderLedger(body.ledger);
    renderExpenses(body.ledger);
    // Журнал ДОписываем и никогда не перерисовываем: «Реестр» и «Расходы» — это
    // вид на текущее состояние, а журнал — история, её нельзя пересобрать заново.
    appendJournal(body.log);
    return _json({ ok: true });
  } finally {
    lock.releaseLock();
  }
}

// ---------- Отрисовка для глаз ----------
function renderLedger(ledger) {
  var sh = _sheet(LEDGER_SHEET, false);
  sh.clear();

  var wallet = ledger.wallet || {};
  var held = wallet.held || {};
  var assets = ledger.assets || {};
  var recv = ledger.receivables || {};

  var working = _num(wallet.working);
  var heldTotal = 0, assetsTotal = 0, recvTotal = 0;
  Object.keys(held).forEach(function (k) { heldTotal += _num(held[k]); });
  Object.keys(assets).forEach(function (k) { assetsTotal += _num(assets[k]); });
  Object.keys(recv).forEach(function (k) { recvTotal += _num(recv[k]); });

  var rows = [];
  rows.push(['РЕЕСТР', '', 'обновлено: ' + (ledger.updated_at || '')]);
  rows.push(['', '', '']);
  rows.push(['Рабочий баланс', working, 'свободные деньги Ильи и Дмитрия']);
  rows.push(['', '', '']);
  rows.push(['В управлении', heldTotal, 'чужие деньги, лежат у нас']);
  Object.keys(held).forEach(function (k) { rows.push(['  ' + k, _num(held[k]), '']); });
  rows.push(['', '', '']);
  rows.push(['Активы', assetsTotal, '']);
  Object.keys(assets).forEach(function (k) { rows.push(['  ' + k, _num(assets[k]), '']); });
  rows.push(['', '', '']);
  rows.push(['Дебиторка', recvTotal, 'нам должны']);
  Object.keys(recv).forEach(function (k) { rows.push(['  ' + k, _num(recv[k]), '']); });
  rows.push(['', '', '']);
  rows.push(['НАШИ АКТИВЫ (Илья + Дмитрий)', working + assetsTotal + recvTotal,
             'рабочий баланс + активы + дебиторка; чужие не входят']);

  sh.getRange(1, 1, rows.length, 3).setValues(rows);
  sh.getRange(1, 2, rows.length, 1).setNumberFormat('#,##0 $');
  sh.getRange(1, 1, 1, 3).setFontWeight('bold');
  sh.getRange(rows.length, 1, 1, 3).setFontWeight('bold');
  sh.setColumnWidth(1, 260);
  sh.setColumnWidth(2, 140);
  sh.setColumnWidth(3, 380);
}

// ---------- Журнал: только дописываем ----------
var JOURNAL_HEADERS = ['Дата', 'Время', 'Тип', 'Позиция', 'Было', 'Стало',
                       'Изменение', 'Наши активы после', 'Что сказано'];

function appendJournal(entries) {
  if (!entries || !entries.length) return;
  var sh = _sheet(JOURNAL_SHEET, false);
  if (sh.getLastRow() === 0) {
    sh.appendRow(JOURNAL_HEADERS);
    sh.getRange(1, 1, 1, JOURNAL_HEADERS.length).setFontWeight('bold');
    sh.setFrozenRows(1);
    sh.setColumnWidth(4, 220);
    sh.setColumnWidth(9, 420);
    // Дату и время держим ТЕКСТОМ: иначе Sheets распознает «2026-07-16» как дату
    // и вернёт боту Date вместо строки. ISO ещё и сортируется хронологически.
    sh.getRange(1, 1, sh.getMaxRows(), 2).setNumberFormat('@');
  }
  var rows = entries.map(function (e) {
    var at = String(e.at || '');            // "2026-07-16 14:32" по Москве
    var parts = at.split(' ');
    return [
      parts[0] || '', parts[1] || '',
      e.kind === 'correction' ? 'правка данных' : 'операция',
      String(e.label || e.path || ''),
      _num(e.before), _num(e.after), _num(e.amount), _num(e.our_assets),
      String(e.summary || ''),
    ];
  });
  var start = sh.getLastRow() + 1;
  sh.getRange(start, 1, rows.length, JOURNAL_HEADERS.length).setValues(rows);
  sh.getRange(start, 5, rows.length, 4).setNumberFormat('#,##0 $');
}

/** Последние N строк журнала — для кнопки «Журнал» в боте. */
function readJournal(limit) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(JOURNAL_SHEET);
  if (!sh || sh.getLastRow() < 2) return [];
  var n = Math.min(limit, sh.getLastRow() - 1);
  var vals = sh.getRange(sh.getLastRow() - n + 1, 1, n, JOURNAL_HEADERS.length).getValues();
  return vals.map(function (r) {
    return {
      date: _text(r[0]), time: _text(r[1]), kind: _text(r[2]), label: _text(r[3]),
      before: _num(r[4]), after: _num(r[5]), amount: _num(r[6]),
      our_assets: _num(r[7]), summary: _text(r[8]),
    };
  });
}

/** Ячейку могли когда-то отформатировать датой — тогда тут Date, а не строка. */
function _text(v) {
  if (v instanceof Date) {
    return Utilities.formatDate(v, 'Europe/Moscow', 'yyyy-MM-dd');
  }
  return String(v == null ? '' : v);
}

function renderExpenses(ledger) {
  var sh = _sheet(EXPENSES_SHEET, false);
  sh.clear();
  var recs = ledger.expenses || [];

  var rows = [['Дата', 'Период', 'Кто', 'Рубли', 'Доллары', 'Баланс', 'Комментарий']];
  recs.forEach(function (rec) {
    var by = rec.by_person || {};
    var names = Object.keys(by);
    if (!names.length) names = ['—'];
    names.forEach(function (name, i) {
      rows.push([
        i === 0 ? (rec.date || '') : '',
        i === 0 ? (rec.period || '') : '',
        name,
        _num((by[name] || {}).rub),
        _num((by[name] || {}).usd),
        // Списан расход с рабочего баланса или только записан. Без этого столбца
        // через месяц по сумме уже не понять, учтён он в балансе или нет.
        i === 0 ? (rec.deducted ? 'списан' : 'только запись') : '',
        i === 0 ? (rec.note || '') : '',
      ]);
    });
    var tail = [];
    if (_num(rec.covered_by_profit_rub)) {
      tail.push('покрыто прибылью ' + _fmt(rec.covered_by_profit_rub) + ' ₽');
    }
    var paid = rec.paid_from_working || {};
    if (_num(paid.usd)) {
      tail.push('с рабочего баланса ' + _fmt(paid.rub) + ' ₽ = ' + _fmt(paid.usd) + ' $');
    }
    rows.push(['', '', 'ИТОГО', _num(rec.total_rub), _num(rec.total_usd), '', tail.join('; ')]);
    rows.push(['', '', '', '', '', '', '']);
  });

  if (recs.length === 0) rows.push(['', '', 'расходов пока нет', '', '', '', '']);
  sh.getRange(1, 1, rows.length, 7).setValues(rows);
  sh.getRange(1, 1, 1, 7).setFontWeight('bold');
  sh.getRange(2, 4, rows.length - 1, 1).setNumberFormat('#,##0 ₽');
  sh.getRange(2, 5, rows.length - 1, 1).setNumberFormat('#,##0 $');
  sh.setColumnWidth(1, 100);
  sh.setColumnWidth(2, 110);
  sh.setColumnWidth(3, 160);
  sh.setColumnWidth(6, 110);
  sh.setColumnWidth(7, 420);
}
