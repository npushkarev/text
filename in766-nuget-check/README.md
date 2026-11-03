# IN-766: проверка NuGet.Protocol против ProGet

Скрипт сравнивает, как к ProGet ходят `dotnet restore` и код из IN-766 (`Settings.LoadDefaultSettings(root: null)` + `NuGet.Protocol`).
Всё выполняется в том же образе `proget.inc.elara.local/main/library/dotnet:0.5.0`.

## Что нужно

- Linux с Docker, пользователь в группе `docker`.
- Доступ к `proget.inc.elara.local`. Если образ ещё не скачан: `docker login proget.inc.elara.local`.
- В образе должен быть .NET SDK, а не только runtime.
- В фиде должны быть пакеты `NuGet.Protocol` и `NuGet.Commands` версии `NUGET_VERSION`.

## Запуск

```sh
git clone https://github.com/npushkarev/text.git
cd text/in766-nuget-check
./in766-nuget-check.sh
```

С клоном OpenIDE Backend из ветки `bugfix/EIDE-670-code-gen-unit-test-minal-failing-app` и учёткой ProGet:

```sh
REPO=<путь_к_клону> PROGET_USER='<логин>' PROGET_PASSWORD='<пароль>' ./in766-nuget-check.sh
```

| Переменная | По умолчанию | Что это |
|---|---|---|
| `IMAGE` | `proget.inc.elara.local/main/library/dotnet:0.5.0` | образ с dotnet |
| `FEED` | `http://proget.inc.elara.local/nuget/Default/v3/index.json` | фид для проверки |
| `NUGET_VERSION` | `6.12.1` | версия `NuGet.Protocol` для проверочного приложения |
| `REPO` | нет | клон репозитория, чтобы проверить его `nuget.config` |
| `PROGET_USER`, `PROGET_PASSWORD` | нет | учётка, если фид закрыт |

Лог пишется в `in766-<дата>.log` в текущем каталоге. Пароли и строки с `password` в лог не попадают.

## Что проверяется

1. `dotnet restore` проверочного приложения из источников образа.
2. Прямой HTTP-запрос к `FEED` и какой прокси для него выбирает .NET.
3. Код из IN-766 как есть: `root: null`.
4. То же плюс `DefaultCredentialServiceUtility.SetupDefaultCredentialService`, как делает CLI.
5. NuGet.Protocol с явным конфигом, где указан только `FEED`.
6. С `REPO`: NuGet.Protocol с `root` = клон репозитория.

В конце строка `ИТОГ` и подсказка, какой вариант помог.
Для каждого источника печатается URL, есть ли учётка, и полная цепочка исключений.

На Astra не будет того, что добавляет TeamCity: build feature «NuGet feed credentials», `NUGET_PLUGIN_PATHS`, переменные агента.
Если на Astra всё работает, сравните вывод шага «окружение» с параметрами сборки 475660 в TeamCity.

## Как TeamCity, не от root

TeamCity может запускать контейнер не от root и с другим `HOME`. Тогда конфиг `/root/.nuget/NuGet/NuGet.Config` из образа не виден.
Проверка того же на Astra:

```sh
DOCKER_USER=1000:1000 ./in766-nuget-check.sh
```

Если шаг 3 падает, а шаг 5 проходит, причина в том, где лежит конфиг NuGet, а не в ProGet.
