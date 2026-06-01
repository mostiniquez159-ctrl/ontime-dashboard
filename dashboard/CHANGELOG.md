# CHANGELOG — onTime Dashboard v3

## [2026-06-01] CHG-20260601-NAV-01
- type: fix
- scope: navigation
- description: Удалён захардкоженный блок "Проекты" из левого меню.
  Содержал устаревшие записи: Плюс Лого, onTime.ai, Андрей Самсонов,
  Ольга Федеева, Внешние клиенты. Клиенты управляются через Контент-завод.
- files: app.py
- status: deployed

## [2026-06-01] CHG-20260601-CF-01
- type: feat
- scope: content-factory
- description: Добавлен таб "Стратегия" в Контент-завод между "Обзор" и "Контент".
  Блоки: Конкуренты (список + парсинг), Источники трафика (read-only из Маркетинга),
  База знаний (kb_entries из опубликованных постов).
- files: app.py
- status: deployed
