# Аудит: mkv (smart AV1 + Opus compression)

- Дата: 2026-09-11
- Ревьюер: Auditor (Senior Code Forensics / AppSec QA)
- Объект: `mkv_encode.py` (553 строки), обёртки `mkv` / `mkvf`, `README.md`
- Коммит: `f372431` ("Smart AV1+Opus compression with per-track audio bitrate search")

## Вердикт: [APPROVED] (после перепроверки)

Причина: нет тестов (п. 5 чек-листа — автоматический REJECT) + один
критичный дефект надёжности (падение на повреждённом кэше). Детали — в таблице.

| Блок кода | Тип | Описание | Как исправить |
|---|---|---|---|
| репозиторий целиком | Крит | Нет тестов ни для одной функции. П. 5 чек-листа требует тесты, без них — REJECT | Добавить `tests/` с юнит-тестами чистых функций (`bitrate_bounds`, `ladder_for`, `probe` на фикстурах ffprobe-json) и тестами с моками `subprocess` для `estimate_src_bitrate`, `active_channels`, `measure_sisdr`, `pick_audio_bitrate`, `run_crf_search`, валидации аргументов `main`. Минимум: `pytest`, запуск одной командой |
| `main`, строки 412–418 (чтение кэша) | Крит | Повреждённый/подброшенный JSON в world-writable `/tmp` роняет скрипт трейсбеком: при `audio` типа строка/словарь/короткий список list-comprehension `[(a[0], a[1]) ...]` бросает `IndexError`, а ловится только `(OSError, ValueError, TypeError, KeyError)`. Проверено вживую: `'"ab"'`, `'{"x": 1}'`, `'[["a"]]'` → `IndexError` | Добавить `IndexError` (и `AttributeError`) в except либо валидировать форму записей: `if isinstance(a, (list, tuple)) and len(a) == 2` |
| `main`, шаг 4 (encode) | Warning | Остаток `_tmp_encode` от убитого запуска (`SIGKILL`, обрыв питания) при старте не чистится — чистка есть только на путях fail/interrupt. Следующий `ab-av1 encode` может отказаться писать в существующий файл | После `.bak`-подтверждения, до кодирования: если `tmp_output` существует — удалить (или спросить). Зеркало старого `trap ... rm -f` |
| `run_crf_search`, строки 323–353 | Warning | При `KeyboardInterrupt`/исключении во время чтения `p.stdout` дочерний `ab-av1` не завершается (`p.kill()` нет) — orphan-энкодер жжёт CPU. `p.stdout.close()` в `finally` ребёнка не останавливает | Обернуть цикл в `try/finally` с `p.kill(); p.wait()` при нештатном выходе |
| `eprint` + все сообщения | Warning | В строках символы `→`, `—` (строки 467, 472 и др.). Под локалью без UTF-8 (`LANG=C`, ASCII stderr) любой `print` такого сообщения бросит `UnicodeEncodeError` и замаскирует реальную ошибку | Либо только ASCII в выводе, либо `sys.stderr.reconfigure(errors="replace")` на старте |
| `pick_audio_bitrate`, max-ветка | Warning | Если фильтра `asisdr` нет в сборке ffmpeg, `measure_sisdr` вернёт `None` для ВСЕХ кандидатов — код молча упадёт на потолок (`max`) вместо честного `fallback`. Пользователь увидит «потолок», а не «замер недоступен» | Один раз проверить наличие фильтра (`ffmpeg -h filter=asisdr`) и сразу уходить в `fallback` |
| две `os.rename` подряд, строки 540–541 | Warning | Замена неатомарна: падение между переименованиями оставит оригинал только в `.bak` (восстановимо вручную, но молча) | Приемлемо как есть; минимум — строчка в README про ручное восстановление. Лучше: `os.replace` + fsync dir (opt) |
| `ladder_for`, строки 152–159 | Opt | Докстринг обещает «минимум 2 ступени», но при `lo == hi` внутри лестницы (`ladder_for(64, 64)` → `[64]`, проверено) возвращается 1. На корректность не влияет | Починить докстринг или код |
| `pick_audio_bitrate`, строка 317 | Opt | Голый `except Exception: pass` прячет настоящие баги (зато честно падает в `fallback`) | Хотя бы `eprint(f"audio search failed: {e}")` в stderr |
| `measure_sisdr` + `round(float('inf'), 1)` | Opt | Полностью немая дорожка печатает «SI-SDR inf dB». В JSON это не попадает (кэшируются только `br`/`method`), но хрупко: начни кэшировать `score` — получишь невалидный JSON (`Infinity`) | Комментарий-сторожок или `score = None if score == inf` перед кэшированием |
| `doc/roadmap.md` | Opt | Файла нет — сверить реализацию со спецификацией (п. 6 чек-листа) невозможно | Завести `doc/roadmap.md` или убрать пункт из пайплайна |
| репозиторий | Opt | Нет `.gitignore`; `python3 -m py_compile` / прогоны создают `__pycache__/`, который легко закоммитить по ошибке | Добавить `.gitignore` с `__pycache__/` |

## Что проверено и признано годным

1. **Память/ресурсы.** Утечек нет: сэмплы — в `TemporaryDirectory` (авточистка);
   `p.stdout.close()` в `finally`; `tmp_output` удаляется на путях fail/Ctrl+C
   (кроме замечания про stale-файл выше). Double-free / use-after-free
   неприменимы (Python). Дескрипторы `Popen` закрываются.
2. **Безопасность.** `shell=True`, `os.system`, `eval/exec`, `pickle` — отсутствуют
   (проверено `grep`). Все запуски — списки аргументов без шелла, инъекции через
   имя файла/трека невозможны. Пути через `realpath`, кэш пишется атомарно
   (`.tmp` + `os.replace`), секретов в кэше нет (только числа). Tmp-файлы —
   `TemporaryDirectory` (0700).
3. **Сложность.** Аудиопоиск — `O(T × C)` коротких энкодов 60-с сэмпла
   (T — дорожки, C — ступени лестницы, поиск снизу вверх = минимальные байты);
   на фоне `crf-search` видео (минуты) — пренебрежимо. Лишних аллокаций нет.
4. **Читаемость.** Именование, докстринги, комментарии — хорошие; guard-клаузы
   на месте; логика `active_channels` (порог −60 дБ, защита от суммарных строк
   `astats` через `cur == len(rms)`) — корректна, подтверждена прогоном
   (`[0, 1]` на 5.1-сэмпле). Смешанный Unicode в выводе — см. Warning выше.
5. **Поведение подтверждено прогонами ранее:** stereo 80k / 5.1 256k (film),
   64k / 192k (animation); opus 48k → `copy` end-to-end; `.bak`-промпт с `n` —
   чистый выход 1; файл без аудио — OK; `5.1(side)` — OK.
6. **ROADMAP.** `doc/roadmap.md` отсутствует — проверка невозможна (см. Opt).

## Что должен сделать Coder

1. Добавить `tests/` + `pytest` (Крит, блокер).
2. Починить чтение кэша — `IndexError`/валидация формы (Крит, блокер).
3. Желательно: чистка stale-tmp на старте, `p.kill()` в `run_crf_search`,
   ASCII-безопасный вывод или `errors="replace"`, проверка наличия `asisdr`,
   `.gitignore`, `doc/roadmap.md`.

После исправлений — вернуть на перепроверку (re-audit).

## Перепроверка (re-audit): 2026-09-11

### Итог перепроверки: [APPROVED]

Прогон: `python3 -m pytest tests/ -q` → **54 passed**.
Плюс живые проверки через настоящие функции (не моки):
устойчивость `load_cache` к 9 битым пейлоадам — OK,
хороший кэш читается — OK, `ladder_for` всегда ≥ 2 ступеней — OK,
`asisdr` в сборке есть, `_json_safe_score` — OK.

| Пункт первого аудита | Статус | Чем подтверждено |
|---|---|---|
| Тесты (Крит) | Закрыт | `tests/test_mkv_encode.py`, 54 теста на каждую функцию: `eprint`, `run_quiet`, `probe`, `bitrate_bounds`, `ladder_for`, `estimate_src_bitrate`, `extract_sample`, `encode_opus`, `active_channels`, `measure_sisdr`, `_json_safe_score`, `load_cache`, `pick_audio_bitrate` (copy/search/max/fallback), `run_crf_search` (парсинг, `kill`), валидация `main`. Запуск одной командой, внешних файлов/сети не требуют |
| Падение на битом кэше (Крит) | Закрыт | Чтение вынесено в `load_cache()` с валидацией каждой записи + `IndexError`/`AttributeError` в except. Все пейлоады первого аудита (`'"ab"'`, `'{"x": 1}'`, `'[["a"]]'` и др.) возвращают `(None, None)` — проверено вживую |
| Stale `_tmp_encode` (Warning) | Закрыт | Удаление остатка после `.bak`-подтверждения, с ошибкой — выход 1 |
| Orphan `ab-av1` (Warning) | Закрыт | `except BaseException` → `p.kill()` → `raise`; вызов `kill` покрыт тестом |
| Unicode при ASCII-локали (Warning) | Закрыт | `stderr`/`stdout` → `errors="replace"` в начале `main` |
| Нет `asisdr` → слепой потолок (Warning) | Закрыт | Проверка `ffmpeg -h filter=asisdr`; без фильтра — честный `fallback` (покрыт тестом) |
| Неатомарная замена (Warning) | Закрыт (минимум) | `os.rename` → `os.replace` + строка про восстановление из `.bak` в README |
| `ladder_for` / докстринг (Opt) | Закрыт | Код реально гарантирует ≥ 2 ступени (заимствование соседа по лестнице); проверено тестом и вживую |
| Голый `except` (Opt) | Закрыт | Причина пишется в stderr перед fallback (проверено `capsys`-тестом) |
| `inf` в score (Opt) | Закрыт | `_json_safe_score()`: нефинитное → `None`; используется в обеих ветках |
| `.gitignore` (Opt) | Закрыт | `__pycache__/`, `*.pyc`, `.pytest_cache/` |
| `doc/roadmap.md` (Opt) | Открыт, не блокирует | Файла по-прежнему нет; создание — работа Architect, Coder правомерно не трогал |

Новых дефектов, внесённых исправлениями, не найдено:
инъекций/шелла нет, все вызовы — списки аргументов;
`_stream.reconfigure` в узком except; удаление stale-tmp стоит после
`.bak`-промпта и до чтения кэша; проверка `asisdr` — без глобалов
(один быстрый вызов на дорожку).
