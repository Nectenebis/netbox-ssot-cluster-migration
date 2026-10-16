# netbox-vm-reconcile

Самостоятельный preflight для **NetBox 4.7.x** и **bl4ko/netbox-ssot v1.26.0**.
Сохраняет NetBox VM ID после перемещения между VMware-кластерами и ESXi,
а также при переименовании. Не изменяет оригинальный SSoT.

Приложение читает vCenter и обновляет **только** `name`, `cluster`, `device`, `site`
существующей VM через частичный PATCH. Оно не создаёт и не удаляет объекты,
не переносит MAC/IP, не изменяет интерфейсы, ресурсы, tenant, tags или custom fields.
Старый ID и связанные с ним ресурсы остаются на месте.

## Требования и границы

* Linux, Python 3.11+, HTTPS-доступ к NetBox и всем настроенным vCenter.
* Учётная запись vCenter с read-only доступом ко всему выбранному inventory.
* NetBox token с view для VM/Cluster/Device и change для VirtualMachine.
  Создание/удаление объектов и изменение интерфейсов не требуется.
* ESXi Device и Cluster уже существуют в NetBox; Device назначен правильному
  Cluster и Site. Поддерживаются кластеры без scope и со scope `dcim.site`.
  Другие scope останавливают обработку соответствующей VM.
* Имя `vmware[].name` точно соответствует имени VMware source в SSoT.
  Для VM обязательны `custom_fields.source == name`, tag `vmware` и source tag
  (по умолчанию `Source: <name>`). Смешанные известные source type tags запрещены.
* Кластеры и ESXi находятся автоматически: сначала по `custom_fields.source` +
  `custom_fields.source_id` (VMware MoRef), затем по уникальному имени внутри source,
  если у найденного объекта нет source_id. Проверяются VMware type и source tags.
  Совпадение имени с другим source_id считается конфликтом. Ручные `cluster_ids`
  и `host_ids` необязательны и имеют приоритет как явно заданные исключения.
* Имена VM должны совпадать с результатом SSoT. Если SSoT применяет преобразования
  имён, фильтры inventory или синхронизирует standalone ESXi, сначала согласуйте
  области синхронизации. Здесь standalone inventory останавливает preflight,
  templates по умолчанию исключены (`ignore_templates: true`). Нет regex-фильтров.
* Любая незарегистрированная VM блокирует обычный запуск. Новые VM сначала
  регистрируются через управляемый оператором первый проход SSoT и bootstrap;
  reconciler сам не создаёт VM. Это сознательное ограничение безопасности.

Работа с реальными NetBox/vCenter в этой среде не проверена. До включения apply
нужны dry-run и испытание на отдельной VM в вашей инфраструктуре.

## Идентичность и SQLite

Первичный ключ: `(vCenter about.instanceUuid, VM config.instanceUuid)`.
UUID нормализуются. BIOS UUID (`config.uuid`) сохраняется как дополнительный
предохранитель; его изменение требует ручного разбора. Дубликаты instance UUID
внутри vCenter или BIOS UUID во всём inventory блокируют весь запуск.

`instanceUuid` назначается vCenter и различает экземпляры, в том числе при
совпадающих BIOS UUID. BIOS UUID виден гостю и может копироваться/меняться при
клонировании и регистрации. MoRef (`vm-123`) локален vCenter и может меняться при
повторной регистрации: он используется только для чтения объекта в текущей сессии.
Обычный vMotion между кластерами одного vCenter сохраняет идентичность. При
клонировании ожидаются новые UUID; при cross-vCenter migration сохранность UUID
зависит от операции и версии, поэтому автоматического переноса mapping нет.
Нельзя выводить тождество VM из одного BIOS UUID или имени.

UUID самого vCenter (`content.about.instanceUuid`) получается автоматически при
подключении. `bootstrap --dry-run` показывает новые UUID без записи;
`bootstrap --apply` сохраняет их в SQLite, включая vCenter без VM. После этого
смена UUID у того же source, повторное использование UUID другим source или
удаление зарегистрированного source из конфигурации блокируют запуск до ручного
разбора. Изменение DNS само по себе не меняет идентичность. Обычный reconcile
никогда не регистрирует новый vCenter автоматически. Первая регистрация доверяет
серверу, к которому вы настроили подключение; проверьте адрес перед bootstrap.

`expected_uuid` теперь необязателен: при желании это дополнительная ручная проверка
поверх SQLite. Убирать его из существующего YAML можно — сохранённый UUID остаётся.
PowerCLI для обычной настройки больше не нужен. Для диагностики UUID и MoRef:

```powershell
$si = Get-View ServiceInstance
$si.Content.About.InstanceUuid
Get-VM | Select Name, @{N='InstanceUuid';E={$_.ExtensionData.Config.InstanceUuid}},
  @{N='BiosUuid';E={$_.ExtensionData.Config.Uuid}}, Id
Get-Cluster | Select Name, @{N='MoRef';E={$_.ExtensionData.MoRef.Value}}
Get-VMHost | Select Name, @{N='MoRef';E={$_.ExtensionData.MoRef.Value}}
```

Для YAML используйте голый MoRef (`domain-c101`, `host-201`), без префикса
PowerCLI `ClusterComputeResource-` или `HostSystem-`. После настройки команда
`inventory` выводит текущие UUID и MoRef без обращения к NetBox.

SQLite содержит source, оба UUID VM, NetBox ID, последнее имя/кластер и timestamps.
Отдельная таблица `vcenters` закрепляет уникальную связь source → vCenter UUID.
Ограничения PRIMARY KEY и UNIQUE обеспечивают связь один-к-одному. Каждая запись
фиксируется отдельной транзакцией с `synchronous=FULL`. Автоматического удаления
или перепривязки нет. Временно отсутствующие VM сохраняются с предупреждением.
Отсутствующая/повреждённая БД блокирует запуск; обычная команда не создаёт её заново.
`mappings init --apply` — явная операторская операция, которую нельзя включать в timer.

## Установка Linux

Остановите timer на время первичной настройки. Основной unit и бинарник SSoT
не заменяются. Выполняйте команды установки с правами администратора; примеры
предполагают сервисного пользователя `netbox-ssot` (замените своим из unit).

```bash
systemctl stop netbox-ssot.timer
git clone https://github.com/Nectenebis/netbox-ssot-cluster-migration.git /opt/netbox-vm-reconcile
cd /opt/netbox-vm-reconcile
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements.txt
.venv/bin/python -m pip install --no-deps .
install -d -m 0750 -o netbox-ssot -g netbox-ssot /var/lib/netbox-vm-reconcile
install -d -m 0750 -o root -g netbox-ssot /etc/netbox-vm-reconcile
install -m 0640 -o root -g netbox-ssot config.example.yaml /etc/netbox-vm-reconcile/config.yaml
```

Отредактируйте YAML: реальные URL/hostname, source, переменные секретов и настройки TLS.
Полный образец: [config.example.yaml](config.example.yaml). Несколько vCenter
задаются дополнительными элементами `vmware`, каждый со своими переменными.
Краткий пример — ручные UUID, Cluster ID и Host ID не требуются:

```yaml
netbox:
  url: https://netbox.example.local
  token: ${NETBOX_TOKEN}
  timeout: 30
state:
  database: /var/lib/netbox-vm-reconcile/state.db
vmware:
  - name: mvcenter
    hostname: mvcenter.example.local
    username: ${VCENTER_USER}
    password: ${VCENTER_PASSWORD}
    verify_ssl: true
reconcile:
  allow_rename: true
  allow_cluster_change: true
  allow_host_change: true
  allow_site_change: true
```

Секреты допускаются только как `${VARIABLE}`. Создайте файл
`/etc/netbox-vm-reconcile/credentials.env` с правами `0640 root:netbox-ssot`:

```ini
NETBOX_TOKEN=<secret>
VCENTER_USER=<read-only-account>
VCENTER_PASSWORD=<secret>
```

Не помещайте значения в Git, командную строку или логи. Для ручных команд ниже
сначала безопасно передайте эти переменные в окружение сервисного пользователя
через ваш менеджер секретов или защищённую сессию. Пример EnvironmentFile рассчитан
на systemd, не на исполнение через shell `source`.

TLS проверяется по умолчанию. Для внутреннего CA укажите `ca_file` в `netbox` и
соответствующем элементе `vmware`. Чтобы отключить проверку сертификата и hostname
только у конкретного vCenter, укажите в его секции `verify_ssl: false`. HTTPS
сохраняется, но подлинность сервера по сертификату не проверяется; приложение
выдаёт предупреждение. Проверка сохранённого UUID продолжает работать, однако
она не заменяет проверку TLS. `ca_file` при отключённой проверке vCenter игнорируется.
NetBox TLS остаётся обязательным. vSphere использует прямой HTTPS SDK (не HTTP REST);
сеть/прокси должны поддерживать этот маршрут.

Если SSoT переименовывает Cluster/Device и нет source_id, либо placement-объекты
не имеют source metadata/tags, можно явно указать исключения внутри нужного vCenter:

```yaml
cluster_ids: {domain-c101: 10, domain-c102: 20}
host_ids: {host-201: 101, host-202: 102}
```

Слева — VMware MoRef, справа — ID существующего объекта NetBox. Для ESXi внутри
кластеров автоматически определяется Device, а Site берётся из него. При межкластерной
миграции Cluster, Device и Site обновляются согласованно; standalone ESXi не поддержаны.

## Обновление с 0.1.x

Схема SQLite обновлена с v1 до v2 для сохранения vCenter UUID. Не создавайте БД заново.
Остановите timer и дождитесь завершения service. **До обновления кода** сделайте
backup командой старой версии `mappings backup /secure/backup/state-before-v2.db --apply`.
Если код уже обновлён, резервную копию v1 можно сделать штатным SQLite backup API.
Затем обновите код и пакет:

```bash
cd /opt/netbox-vm-reconcile
git pull --ff-only origin main
.venv/bin/python -m pip install --require-hashes -r requirements.txt
.venv/bin/python -m pip install --no-deps .
.venv/bin/netbox-vm-reconcile mappings migrate --apply
.venv/bin/netbox-vm-reconcile bootstrap --dry-run
.venv/bin/netbox-vm-reconcile bootstrap --apply
.venv/bin/netbox-vm-reconcile reconcile --dry-run
```

Как и остальные рабочие команды, migrate/bootstrap выполняются под сервисным
пользователем; bootstrap требует секреты в окружении. Миграция локальная и не
подключается к API: она переносит UUID из существующих VM mapping, не меняя ни
одного NetBox ID. Противоречивые старые source/UUID приводят к откату всей миграции.
Повторный migrate для v2 ничего не меняет. Ни dry-run, ни обычный reconcile не
мигрируют БД. Для новой установки используйте `mappings init`, а не migrate.

## Первичная регистрация

При остановленном timer и отсутствии параллельных SSoT-процессов, под сервисным
пользователем с подготовленным окружением:

```bash
R=/opt/netbox-vm-reconcile/.venv/bin/netbox-vm-reconcile
$R mappings init --apply
$R inventory
$R bootstrap --dry-run
$R bootstrap --apply
$R mappings list
$R reconcile --dry-run
$R reconcile --apply
$R check
```

`--config` и `--verbose` можно указать до или после команды. `--apply` и
`--dry-run` взаимоисключающие. Без `--apply` remote PATCH и SQLite UPDATE/INSERT
не выполняются. Dry-run читает БД read-only; остаётся только файловая блокировка
в существующем state directory (подтверждённые mapping не меняются).

Bootstrap ищет **единственное** совпадение имени во всём NetBox и VMware inventory,
проверяет target Cluster и ownership. Совпадение одного имени недостаточно.
Bootstrap пишет только SQLite, никогда не PATCH-ит NetBox. Повторный запуск
проверяет уже сохранённые связи. Неоднозначности приводят к коду 2; однозначные
связи при apply могут быть зарегистрированы, остальные требуют ручной проверки.
UUID успешно прочитанных vCenter сохраняются при bootstrap apply независимо от
наличия VM-конфликтов. Частично успешный bootstrap всё равно возвращает 2.

Если VM уже мигрировала, оператор после проверки UUID, MAC/IP и истории может
явно подтвердить старый правильный ID:

```bash
$R mappings bind --source mvcenter --instance-uuid <instance-uuid> --netbox-id 3219 --dry-run
$R mappings bind --source mvcenter --instance-uuid <instance-uuid> --netbox-id 3219 --apply
$R reconcile --dry-run
```

Bind не обходит ownership и не заменяет уже существующую связь. Даже явно указанный
ID не позволяет игнорировать существующий дубль. Команда `conflicts` запускает
диагностику обычного reconcile без записи. При `DUPLICATE DETECTED` выводятся UUID,
ID и Cluster кандидатов. Например, после анализа вручную удалите неправильный
пустой дубль 3567 средствами NetBox, сохраните 3219 с интерфейсами/MAC/IP, затем
повторите bind/reconcile. Приложение ничего не удаляет.

Изменение UUID, ownership или перенос в другой vCenter требуют ручного расследования,
резервной копии и отдельной контролируемой процедуры обслуживания SQLite. CLI
намеренно не предоставляет force-rebind/unbind или небезопасный JSON import.
Нельзя обнулять всю БД для устранения одиночного конфликта.

## systemd

После успешных bootstrap, dry-run, apply и check:

```bash
install -d /etc/systemd/system/netbox-ssot.service.d
install -m 0644 packaging/systemd/netbox-ssot-reconcile.conf \
  /etc/systemd/system/netbox-ssot.service.d/reconcile.conf
systemctl daemon-reload
systemctl cat netbox-ssot.service
systemctl start netbox-ssot.service
journalctl -u netbox-ssot.service -n 100 --no-pager
systemctl start netbox-ssot.timer
```

Drop-in добавляет `EnvironmentFile`, обязательный `ExecStartPre` и startup timeout;
исходный `ExecStart=/opt/netbox-ssot/netbox-ssot ...` остаётся прежним. Без префикса
`-` systemd не запускает ExecStart, если preflight вернул 2/3. Проверьте, что основной
unit не применяет `SuccessExitStatus=2 3`, что существующие ExecStartPre не запускают
SSoT, а User, filesystem sandbox и общий TimeoutStartSec совместимы с inventory.
`PermissionsStartOnly`/`RootDirectory`/`ReadWritePaths` существующего unit требуют
проверки администратором. Не расширяйте права NetBox token ради этих настроек.

Блокировка `state.db.lock` не даёт двум reconciler одновременно работать с одной
БД. **Она не блокирует SSoT, запущенный вручную, другим unit или с другой БД.**
Для этого проекта NetBox не предоставляет атомарную транзакцию чтения+PATCH:
повторные чтения сужают окно гонки, но не устраняют его. Все writers необходимо
сериализовать операционно. Можно запускать SSoT только из одного unit и ограничить
ручной запуск; для нескольких units используйте общий внешний `flock` wrapper,
который держит блокировку на всём цикле reconciler + SSoT. Оба инструмента должны
пользоваться этим wrapper; его lock path должен отличаться от `state.db.lock`.
Не запускайте такой wrapper из ExecStartPre и не освобождайте его lock перед SSoT.

Если одна VM не прошла проверку, остальные безопасные VM могут быть обновлены,
но общий код будет 2, и штатный SSoT не начнётся. Между NetBox и SQLite нет общей
транзакции: после сбоя между PATCH и записью состояния повторный запуск прочитает
тот же ID, обнаружит уже исправленное размещение и обновит metadata без лишнего PATCH.
В конце прохода inventory VMware повторно читается пакетно: изменение состава,
UUID, имени или размещения во время прохода также блокирует запуск SSoT.
Миграция после заключительной проверки остаётся возможной: на критичное окно
reconciler + SSoT организуйте запрет миграций либо внешнюю координацию с VMware.

## Проверка DevTest → prod62

1. До миграции выполните bootstrap для `mnetbox`, убедитесь в mapping на ID 3219.
   Запишите ID интерфейсов, MAC, IP и VM через read-only NetBox API/интерфейс.
2. Остановите timer и убедитесь, что нет выполняющегося SSoT. Переместите тестовую
   VM в prod62. Целевой ESXi должен уже находиться в правильном NetBox Cluster/Site.
3. Запустите `reconcile --dry-run`: ожидается ID 3219 и PATCH Cluster/Device/Site
   только для отличающихся полей. VM 3567 создаваться не должна.
4. Запустите `reconcile --apply`, затем повторный apply: второй не должен делать PATCH.
5. Сравните ID VM, интерфейсов, MAC/IP с исходными. Запустите штатный unit и
   убедитесь, что SSoT обновляет ту же VM без ошибки Primary MAC. Верните timer.

## Backup, восстановление и откат

```bash
$R mappings backup /secure/backup/state-2026-10-16.db --apply
$R mappings export > /secure/backup/mappings-2026-10-16.json
```

Backup использует SQLite backup API, не перезаписывает существующий файл и создаёт
его с mode 0600. JSON — диагностический экспорт, не автоматический import.
Настройте регулярные резервные копии вне сервера, особенно перед обслуживанием.

Восстановление: остановите timer/service и все ручные writers; проверьте backup
через `PRAGMA integrity_check`. Сохраните текущую БД отдельно. При отсутствии
процессов замените state.db проверенной SQLite backup-копией, сохраните service
owner и mode 0600. Не смешивайте новую БД со старыми `-wal`/`-shm` sidecar-файлами;
при их наличии остановитесь и разберите состояние с администратором SQLite.
Затем выполните mappings list и reconcile --dry-run. Отсутствующие с момента
backup связи регистрируются отдельно; никакого автоматического сброса mapping.

Откат интеграции:

```bash
systemctl stop netbox-ssot.timer
rm /etc/systemd/system/netbox-ssot.service.d/reconcile.conf
systemctl daemon-reload
systemctl cat netbox-ssot.service
```

Сохраните SQLite и её backup. Решение о возобновлении timer принимайте после
разрешения миграционных конфликтов: без reconciler исходная проблема возвращается.
Удаление drop-in не откатывает уже применённое размещение. При необходимости
восстанавливайте конкретные поля через проверенную историю изменений NetBox.
Удалять `/opt/netbox-vm-reconcile` и конфигурацию можно после отключения drop-in;
основной `/opt/netbox-ssot` не затрагивается.

## Разработка и проверки

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python -m pip install --no-deps -e .
.venv/bin/pytest
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/python -m build
```

Unit/fake API тесты покрывают миграции, idempotence, bootstrap, конфликт UUID,
ownership, автоматический placement, регистрацию vCenter, миграцию схемы SQLite,
явное отключение TLS vCenter, dry-run, lock, ошибки и частичный успех. Отдельно проверяется
реальный pynetbox transport с mock HTTP, пагинация, ограничение PATCH и pyVmomi
PropertyCollector/SmartConnect с mocks. Они не заменяют live интеграционные тесты
NetBox validation, vCenter RBAC, TLS внутреннего CA и запуска systemd.

`check` возвращает 2 и при безопасном, но неисправленном drift; `reconcile --dry-run`
возвращает 0 для допустимого плана и 2 при конфликте. `conflicts` также показывает
допустимые планы без применения. Код 3 означает неожиданную ошибку. Транспортные
ошибки санитизируются, тела HTTP и traceback с секретами не журналируются.

Зависимости зафиксированы с хешами в requirements*.txt; обновляйте их отдельно
после тестов. Источники проверки API и детали совместимости: [docs/api-research.md](docs/api-research.md).
