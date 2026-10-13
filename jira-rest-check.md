# Проверка Jira для шага CHANGELOG в CP050 BUILD COUNTER

Скрипт `jira-rest-check.py` повторяет запросы к Jira, которые делает PowerShell-скрипт стадии
`CP050 BUILD COUNTER`. По каждому запросу он показывает код ответа и под каким пользователем
Jira его приняла. Так видно, какой запрос ломает сборку.

Ничего не меняет в Jira: все запросы только читают данные.

## Что нужно

- Python 3.6 или новее. Дополнительные пакеты не нужны.
- Доступ к Jira с той машины, где запускаете. Лучше запускать на агенте TeamCity, где падает сборка.

## Как скачать

С доступом к GitHub:

```sh
curl -fsSLO https://raw.githubusercontent.com/npushkarev/text/add-hello-txt/jira-rest-check.py
```

Без доступа: скачайте файл в браузере и перенесите на агент. Проверьте, что имя файла
`jira-rest-check.py`, а не `jira-rest-check.txt` или `jira-rest-check(1).py`.

## Как запустить

```sh
python3 jira-rest-check.py --jira '<Jira.Address>' --project '<ProjectKey>' --user '<Jira.Username>'
```

Скрипт спросит пароль. Символы при вводе не видны, это нормально.
Пароль не пишите в команду и не отправляйте в чат.

Значения возьмите в TeamCity: сборка `CP050 BUILD COUNTER` > вкладка `Parameters`.

| Параметр скрипта | Параметр TeamCity | Сейчас |
|---|---|---|
| `--jira` | `Jira.Address` | `http://jira.inc.elara.local` |
| `--project` | `ProjectKey` | `CS` |
| `--user` | `Jira.Username` | `jira-service` |
| пароль по запросу | `Jira.Password` | пароль учётки `jira-service` |

Пример для CS_PRODUCT:

```sh
python3 jira-rest-check.py --jira 'http://jira.inc.elara.local' --project 'CS' --user 'jira-service'
```

Дополнительные флаги:

- `--token`: вход по Personal Access Token вместо пароля. Скрипт спросит токен.
- `--ca-file /путь/к/ca.crt`: файл корпоративного CA, если Jira на `https` и агент не доверяет её сертификату.

## Что проверяется

Все адреса идут через `<Jira.Address>/rest/api/latest/`, как в скрипте CP050:

| Запрос | Зачем |
|---|---|
| `serverInfo` | версия Jira и Base URL |
| `myself` | принят ли логин |
| `project`, `project/<ключ>` | видит ли учётка проект |
| `project/<ключ>/versions`, `project/<ключ>/version?...` | список релизов |
| `search` (GET и POST) | поиск задач релиза |
| `issue/createmeta` | убран из Jira в версии 9.0, у нас 11.3.11 |
| `user/search?username=...` | поиск пользователя |
| `issue/<задача>`, `.../transitions`, `.../remotelink` | первая найденная задача проекта |

Перенаправления скрипт не выполняет. Если они есть, это видно в выводе.

## Как читать результат

Каждая строка: один запрос. Главное в ней: `code` и `X-AUSERNAME`.

| Что видно | Что значит | Что делать |
|---|---|---|
| Все `"code": 200` | С Jira всё в порядке | Искать причину в другом месте лога сборки |
| `"code": 401`, `AUTHENTICATED_FAILED` | Jira не приняла логин или пароль | Проверить `Jira.Username` и `Jira.Password` в `<Root project>` > `Parameters` |
| `"code": 403` | Логин принят, не хватает прав | Дать учётке права в проекте |
| `"code": 404` у одного запроса | Этого адреса нет в новой Jira | Переписать это место в скрипте CP050 |
| `"code": 404` у `project/...` | Учётка не видит проект или неверный ключ | Проверить `ProjectKey` и права учётки |
| `"code": 301` или `302`, есть `Location` | Адрес перенаправляется | Записать адрес из `Location` в `Jira.Address` |
| `"error": "...CERTIFICATE_VERIFY_FAILED..."` | Агент не доверяет сертификату | Установить корпоративный CA или указать `--ca-file` |
| `"error": "...Connection refused..."` или `timed out` | Jira недоступна с этой машины | Проверить адрес и сеть |

Код выхода скрипта: `0`, если все запросы вернули 200, иначе `1`.

## Если сборка падает с 404

Ошибка `404 ()` в логе TeamCity значит, что логин прошёл, но один из адресов в Jira 11 отсутствует.
Запустите проверку и найдите строку с `"code": 404`. Сфотографируйте вывод.
По этой строке видно, какое место в скрипте стадии нужно исправить.
