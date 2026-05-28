# Пакет `grpc` — полное описание

> Документация по conan-recipe `grpc/` в репозитории `conan-recipes` (тикет IN-658). Самый сложный из 8 наших миграционных рецептов — 487 строк `conanfile.py`, поддержка 4 версий grpc (1.54.3 / 1.60.1 / 1.69.0 / 1.78.1), 6 offline-source tarball'ов, отдельный механизм target_info YAML'ов. Этот документ описывает каждый файл и где можно прокинуть CMake-флаги при сборке.

---

## 1. Структура каталога `grpc/`

```
grpc/                                        ~146 000 строк (с тарболлами)
├── conanfile.py                             487 строк — главный recipe
├── conandata.yml                             16 строк — URL+sha256 upstream-тарболлов
├── conan_cmake_project_include.cmake          4 строки — CMake-инъекция до project()
├── cmake/
│   └── grpc_plugin_template.cmake.in         31 строка — template для install plugin-бинарей
├── target_info/                                       — YAML-таблицы CMake-таргетов per-version
│   ├── grpc_1.54.3.yml                      152 строки (~70 таргетов)
│   ├── grpc_1.60.1.yml                      188 строк (~90 таргетов)  ← наш продакшен
│   ├── grpc_1.69.0.yml                      188 строк
│   └── grpc_1.78.1.yml                      244 строки (~120 таргетов)
├── patches/
│   └── v1.50.x/
│       └── 002-CMake-Add-gRPC_USE_SYSTEMD…   57 строк — единственный патч (для 1.54.x)
├── src/                                               — offline upstream-источники
│   ├── v1.60.1.tar.gz                       66 MB    ← наш текущий продакшен
│   ├── v1.78.1.tar.gz                       58 MB
│   ├── data-plane-api-9d6ffa70…tar.gz      2.4 MB    ← grpc submodule
│   ├── googleapis-2f9af297…tar.gz         16.7 MB    ← grpc submodule
│   ├── opencensus-proto-0.3.0.tar.gz       0.6 MB    ← grpc submodule
│   └── xds-e9ce6880…tar.gz                 0.4 MB    ← grpc submodule
└── test_package/                                      — smoke-test потребителя
    ├── CMakeLists.txt                        22 строки
    ├── conanfile.py                          31 строка
    └── test_package.cpp                       9 строк
```

---

## 2. `conanfile.py` — главный recipe (487 строк)

Класс `GrpcConan(ConanFile)` — Python-описание сборки grpc, читаемое Conan'ом.

### 2.1 Атрибуты класса

| Атрибут | Значение |
|---|---|
| `name` | `"grpc"` |
| `settings` | `"os", "arch", "compiler", "build_type"` |
| `license` | `"Apache-2.0"` |
| `topics` | `("rpc",)` |
| `exports_sources` | `"src/*.tar.gz"` — копируется в Conan-cache при export |

### 2.2 Опции (`options`)

| Опция | Значения | Дефолт | Назначение |
|---|---|---|---|
| `shared` | `True / False` | `False` | static (после миграции на static-by-default) |
| `fPIC` | `True / False` | `True` | position-independent code |
| `codegen` | `True / False` | `True` | генерить ли plugin-бинари (`grpc_cpp_plugin` и др.) |
| `cpp_plugin` | `True / False` | `True` | C++ plugin |
| `csharp_plugin` | `True / False` | `True` | C# |
| `node_plugin` | `True / False` | `True` | Node.js |
| `objective_c_plugin` | `True / False` | `True` | Objective-C |
| `php_plugin` | `True / False` | `True` | PHP |
| `python_plugin` | `True / False` | `True` | Python |
| `ruby_plugin` | `True / False` | `True` | Ruby |
| `secure` | `True / False` | `True` | TLS (через openssl) |
| `use_systemd` | `True / False / "auto"` | `"auto"` | интеграция с libsystemd |
| `with_libsystemd` | `True / False` | (derived) | подтянуть libsystemd как требование |
| `csharp_ext` | `True / False` | `False` | C# extension |

### 2.3 Lifecycle-методы

Conan вызывает их в строгом порядке. Каждый отвечает за свою фазу сборки:

| Метод | Строки | Что делает |
|---|---|---|
| `export()` | 74-77 | Копирует `target_info/grpc_<ver>.yml` в Conan-cache при `conan export` |
| `export_sources()` | 80-83 | Копирует `src/*.tar.gz` в Conan-cache (offline-сборка) |
| `config_options()` | 85-91 | Убирает опции неприменимые на платформе (например `fPIC` на Windows) |
| `configure()` | 93-99 | Делает options consistent: `shared=True` → `fPIC` неактуален, удаляет `cppstd` где не нужен |
| `layout()` | 101-102 | Декларирует использование стандартного `cmake_layout` |
| **`requirements()`** | 104-153 | **Жёсткие пины зависимостей** — самая важная часть, см. §2.4 |
| `package_id()` | 155-156 | Кастомизация hash-вычисления для caching |
| `validate()` | 158-174 | Проверка совместимости settings (падает рано на bad combo) |
| `build_requirements()` | 176-184 | Build-time tools (`cmake`, `ninja`, host-`protobuf` для cross) |
| `source()` | 271-282 | Распаковка `src/*.tar.gz` оффлайн + apply патчи |
| **`generate()`** | 284-342 | CMakeToolchain + CMakeDeps + ARM cross workaround |
| `_patch_sources()` | 344-375 | Доп. правки после patches (`replace_in_file` для динамики) |
| `build()` | 377-381 | `cmake.configure() + cmake.build()` |
| `package()` | 392-407 | `cmake.install()` + упаковка plugin-бинарей |
| **`package_info()`** | 460-487 | Экспорт ~90 CMake-таргетов через `target_info` YAML, см. §2.7 |

### 2.4 `requirements()` — жёсткие version-пины (строки 104-153)

Главное место где живёт логика «какие deps для какой grpc-версии». ABI-критичные пины:

```python
def requirements(self):
    self.requires("zlib/[~1.3]", transitive_headers=True)
    self.requires("c-ares/[*]")
    self.requires("re2/[*]")
    if self.options.secure:
        self.requires("openssl/[*]", transitive_headers=True)

    # protobuf пин по grpc-версии (ABI-совместимость):
    if Version(self.version) >= "1.78":
        self.requires("protobuf/[>=5.27.2 <6]", ...)
    elif Version(self.version) >= "1.60":
        self.requires("protobuf/4.25.2", ...)          # наш продакшен

    # abseil — самое критичное (inline-namespace lts_<date>):
    if Version(self.version) >= "1.78":
        self.requires("abseil/[*]")
    elif Version(self.version) >= "1.69":
        self.requires("abseil/[>=20240116.1 <=20250127.0]")
    elif Version(self.version) >= "1.60":
        # КРИТИЧНО: abseil 20230802.1 (НЕ 20240116.2):
        # downstream slot el_conf/grpc_sdk бинарно линкован против lts_20230802.
        # Другая abseil → inline-namespace mismatch → undefined reference при линке.
        self.requires("abseil/20230802.1")

    # Жёсткая проверка cppstd-match
    abseil_cppstd = self.dependencies.host["abseil"].info.settings.compiler.cppstd
    if abseil_cppstd != self.settings.compiler.cppstd:
        raise ConanInvalidConfiguration("grpc and abseil cppstd mismatch")
```

| grpc-версия | abseil-пин | protobuf-пин |
|---|---|---|
| 1.54.3 | `[>=20230125.3 <=20230802.1]` | (зависит от sub-version) |
| **1.60.1** (продакшен) | **`20230802.1` (жёсткий)** | **`4.25.2` (жёсткий)** |
| 1.69.0 | `[>=20240116.1 <=20250127.0]` | (диапазон 5.x) |
| 1.78.1 | `[*]` | `[>=5.27.2 <6]` |

### 2.5 `generate()` — ARM cross workaround (строки 284-342)

```python
def generate(self):
    tc = CMakeToolchain(self)

    # Conan 2.27.1 не пропагирует tools.cmake.cmaketoolchain:user_toolchain
    # транзитивно при cross-build. Env-fallback гейтит по arch — только для ARM:
    _user_tc = os.environ.get("CONAN_USER_TOOLCHAIN", "").strip()
    if _user_tc and str(self.settings.arch) in ("armv7", "armv7hf", "armv7s",
                                                 "armv8", "armv8_32", "armv8.3",
                                                 "arm64ec"):
        tc.blocks["user_toolchain"].values["paths"] = [_user_tc]

    tc.generate()
    CMakeDeps(self).generate()
```

Эта же конструкция есть в `abseil/conanfile.py`, `re2/conanfile.py`, `protobuf/conanfile.py` — это workaround Conan 2.27.1 quirk. Можно убрать после bump'а Conan.

### 2.6 `_offline_source_archive()` — locale-edit для closed-network (186-232)

Conan по умолчанию делает `wget` URL из `conandata.yml`. На closed-network нет интернета. Helper находит соответствующий tarball локально:

```python
def _offline_source_archive(self):
    """Match conandata.yml URL filename → src/<that-filename>.tar.gz."""
    url = self.conan_data["sources"][self.version]["url"]
    filename = os.path.basename(url)
    candidate = os.path.join(self.recipe_folder, "src", filename)
    if os.path.exists(candidate):
        return candidate
    # fallback: любой *.tar.gz в src/
    for f in os.listdir(os.path.join(self.recipe_folder, "src")):
        if f.endswith(".tar.gz"):
            return os.path.join(self.recipe_folder, "src", f)
    return None

def source(self):
    _local = self._offline_source_archive()
    if _local:
        unzip(self, _local, strip_root=True)
    else:
        get(self, **self.conan_data["sources"][self.version], strip_root=True)
    apply_conandata_patches(self)
```

Этот helper присутствует во всех 8 наших рецептах — единственная (вместе с `exports_sources="src/*.tar.gz"`) **локальная правка** поверх canonical `conan-center-index`.

### 2.7 `_preextract_grpc_offline_deps()` — уникально для grpc (234-269)

`grpc/CMakeLists.txt` при сборке тянет submodule'ы через `FetchContent_Declare` (`data-plane-api`, `googleapis`, `opencensus-proto`, `xds`). На closed-network `git fetch` падает. Метод **до** конфигурации CMake распаковывает их из `src/` в `_deps/<name>-src/`:

```python
def _preextract_grpc_offline_deps(self):
    _deps_root = os.path.join(self.build_folder, "_deps")
    for archive_glob in ["data-plane-api-*.tar.gz", "googleapis-*.tar.gz",
                         "opencensus-proto-*.tar.gz", "xds-*.tar.gz"]:
        path = self._find_in_src(archive_glob)
        target = os.path.join(_deps_root, name.replace("-", "_") + "-src")
        unzip(self, path, destination=target, strip_root=True)
```

Других пакетов с pre-extract'ом submodule'ов в нашем репо нет — это **уникальный механизм для grpc**.

### 2.8 `package_info()` — где `target_info` применяется (460-487)

```python
def package_info(self):
    self.cpp_info.set_property("cmake_file_name", "gRPC")
    self.cpp_info.set_property("cmake_target_name", "gRPC::gRPC")

    for target in self.target_info["grpc_targets"]:    # ← из YAML
        component = self.cpp_info.components[target["name"]]
        if "lib" in target:
            component.libs = [target["lib"]]
            component.set_property("cmake_target_name", f"gRPC::{target['name']}")
        for req in target.get("requires", []):
            component.requires.append(req)
        if "bin" in target:
            self._create_executable_module_file(target["name"], target["bin"])

    self.cpp_info.set_property("cmake_build_modules", [self._module_path])
```

Это **~90 CMake-таргетов для grpc 1.60.1 одним циклом**, без 500-строчного хардкода.

---

## 3. `conandata.yml` — sources + patches metadata (16 строк)

```yaml
sources:
  "1.78.1":
    url: "https://github.com/grpc/grpc/archive/refs/tags/v1.78.1.tar.gz"
    sha256: "961a44a2a5a50670…"
  "1.69.0": # последняя версия с C++14 support
    url: "..."
    sha256: "..."
  "1.60.1": # legacy GR113/120 parity, paired with protobuf:4.25.2 + openssl:1.1.11
    url: "https://github.com/grpc/grpc/archive/refs/tags/v1.60.1.tar.gz"
    sha256: "30f97253703d0070…"
  "1.54.3": # ещё ссылаются другие рецепты
    url: "..."
    sha256: "..."
patches:
  "1.54.3":
    - patch_file: "patches/v1.50.x/002-CMake-Add-gRPC_USE_SYSTEMD-option-34384.patch"
```

**Upstream-as-is** (hard contract — `conandata.yml::sources` идентичен conan-center-index).

---

## 4. `conan_cmake_project_include.cmake` (4 строки)

```cmake
# Включается через tools.cmake.cmaketoolchain:project_include
# в самое начало CMakeLists.txt ПЕРЕД project() — позволяет править
# язык/компилятор/флаги до того как CMake их «зафиксирует».
set(CMAKE_POSITION_INDEPENDENT_CODE ON CACHE BOOL "" FORCE)
```

Обходит специфику grpc'шного CMakeLists.txt — он сбрасывает PIC, что ломает static-сборку library встраиваемых в shared.

---

## 5. `cmake/grpc_plugin_template.cmake.in` (31 строка)

CMake-template (с `@VAR@` плейсхолдерами). При `package()` для каждого plugin-бинаря (`grpc_cpp_plugin`, `grpc_python_plugin`, …) подстановкой генерится **per-target `.cmake` модуль**, объявляющий imported executable:

```cmake
if(NOT TARGET gRPC::@TARGET@)
    add_executable(gRPC::@TARGET@ IMPORTED)
    set_target_properties(gRPC::@TARGET@ PROPERTIES
        IMPORTED_LOCATION "${CMAKE_CURRENT_LIST_DIR}/../../bin/@EXECUTABLE@"
    )
endif()
```

Это делает grpc-плагины импортируемыми у downstream:
```cmake
find_package(gRPC CONFIG REQUIRED)
get_property(plugin_path TARGET gRPC::grpc_cpp_plugin PROPERTY IMPORTED_LOCATION)
add_custom_command(... COMMAND protoc --plugin=protoc-gen-grpc=${plugin_path} ...)
```

---

## 6. `target_info/grpc_<ver>.yml` — таблица CMake-таргетов

YAML-метаданные **отдельно от Python-кода**. Для каждого CMake-таргета grpc:

- `name` — имя для `cpp_info.components["..."]`
- `lib` — имя физической либы (`libgpr.a` / `libgpr.so`)
- `bin` — для plugin-бинарей
- `requires` — inter-package deps в Conan-нотации `"package::component"`

Пример из `grpc_1.60.1.yml`:

```yaml
grpc_version: 1.60.1
grpc_targets:
    - name: "gpr"
      lib: "gpr"
      requires:
        - abseil::absl_base
        - abseil::absl_strings
        - abseil::absl_synchronization
        # …20+ абсейл-компонентов
    - name: "_grpc"
      lib: "grpc"
      requires:
        - zlib::zlib
        - c-ares::cares
        # …
    - name: "_grpc++"
      lib: "grpc++"
      requires:
        - protobuf::libprotobuf
        - _grpc                          # ← на собственный компонент
    - name: "grpc_cpp_plugin"
      bin: "grpc_cpp_plugin"
```

### Почему per-version (а не один файл)

Список grpc-таргетов **сильно меняется** между версиями:

| Версия | Таргетов | Что нового |
|---|---|---|
| 1.54.3 | ~70 | baseline |
| 1.60.1 | ~90 | `absl_log`, `absl_check`, `absl_log_globals` |
| 1.69.0 | ~90 | rebalancing internal |
| 1.78.1 | ~120 | `absl_cleanup`, `absl_bind_front`, `absl_config` |

Один файл потребовал бы `if version >= X:` в YAML, чего YAML не умеет. Per-version — простой straightforward fix.

### Откуда взялись YAML'ы

Извлечение из upstream `grpc/CMakeLists.txt` + `cmake/protobuf.cmake` (парсинг `target_link_libraries()` деклараций). Делается **вручную** при добавлении поддержки новой версии grpc — это не автомат.

---

## 7. `patches/v1.50.x/002-CMake-Add-gRPC_USE_SYSTEMD-option-34384.patch`

Единственный патч (57 строк). Backport upstream PR `grpc#34384` — добавляет CMake-опцию `gRPC_USE_SYSTEMD` для опционального линка с libsystemd. Зарегистрирован **только для grpc 1.54.3** в `conandata.yml::patches`. Для 1.60.1+ не применяется (там уже в upstream).

Применяется автоматически в `source()` → `apply_conandata_patches(self)`.

---

## 8. `src/*.tar.gz` — offline upstream-источники

| Файл | Размер | Назначение |
|---|---|---|
| `v1.60.1.tar.gz` | 66 MB | grpc 1.60.1 источники (наш продакшен) |
| `v1.78.1.tar.gz` | 58 MB | grpc 1.78.1 источники |
| `data-plane-api-9d6ffa70…tar.gz` | 2.4 MB | grpc submodule — Envoy data plane API |
| `googleapis-2f9af297…tar.gz` | 16.7 MB | grpc submodule — Google APIs |
| `opencensus-proto-0.3.0.tar.gz` | 0.6 MB | grpc submodule — OpenCensus protocol buffers |
| `xds-e9ce6880…tar.gz` | 0.4 MB | grpc submodule — Envoy xDS |

**Контракт имени:** filename должен match'ить URL filename из `conandata.yml`. Иначе `_offline_source_archive()` не найдёт и `source()` упадёт.

Submodule-тарболлы используются `_preextract_grpc_offline_deps()` (см. §2.7) — без них grpc сборка пытается `git fetch` и падает на closed-network.

---

## 9. `test_package/` — Conan smoke-тест

Минимальный потребитель — выполняется автоматически в конце `conan create`. Если test_package не собирается — recipe считается сломанным.

```cpp
// test_package.cpp — 9 строк
#include <grpcpp/grpcpp.h>
int main() {
    grpc::ChannelArguments args;
    return 0;
}
```

```cmake
# CMakeLists.txt — 22 строки
find_package(gRPC CONFIG REQUIRED)
add_executable(test_package test_package.cpp)
target_link_libraries(test_package PRIVATE gRPC::grpc++)
```

```python
# conanfile.py — 31 строка
class TestPackageConan(ConanFile):
    settings = "os", "arch", "compiler", "build_type"
    generators = "CMakeDeps", "CMakeToolchain"
    def requirements(self):
        self.requires(self.tested_reference_str)
    def build(self):
        cmake = CMake(self); cmake.configure(); cmake.build()
    def test(self):
        if can_run(self):
            self.run(os.path.join(self.cpp.build.bindir, "test_package"))
```

---

## 10. Где прокидывать CMake-флаги — 7 уровней

7 точек инъекции по убыванию scope (от глобального к локальному). Чем выше уровень — тем шире влияет, тем сложнее переопределить локально.

### Уровень 1 — Conan profile `[conf]` секция

Файл `profiles/lin-gcc84-x86_64` (или другой профайл):

```ini
[settings]
os=Linux
arch=x86_64
compiler=gcc
compiler.version=8.4
compiler.libcxx=libstdc++11
compiler.cppstd=17

[conf]
# 1.1 Сырьевые компиляторные флаги — летят в CFLAGS/CXXFLAGS/LDFLAGS,
#     влияют на ВСЕ пакеты собранные с этим профилем
tools.build:cflags=["-O2", "-g1", "-fno-omit-frame-pointer"]
tools.build:cxxflags=["-O2", "-g1", "-Wno-deprecated-declarations"]
tools.build:sharedlinkflags=["-Wl,--as-needed"]
tools.build:exelinkflags=["-Wl,--as-needed"]

# 1.2 Препроцессорные defines
tools.build:defines=["NDEBUG=1", "GRPC_NO_TLS=0"]

# 1.3 CMake cache-переменные (попадают как -D... в CMake configure)
tools.cmake.cmaketoolchain:extra_variables*={"BUILD_TESTING": "OFF"}

# 1.4 Генератор
tools.cmake.cmaketoolchain:generator=Ninja

# 1.5 ПОЛНЫЙ внешний CMake toolchain (перебивает почти всё ниже)
tools.cmake.cmaketoolchain:user_toolchain=/work/profiles/toolchains/linaro-arm.cmake

# 1.6 Параллелизм для cmake --build
tools.build:jobs=8
```

| Плюсы | Минусы |
|---|---|
| Один профайл — одинаковые флаги для всех пакетов | Нельзя дать grpc особое, не задев zlib/abseil |
| Легко переключается между профайлами (x64 / ARM cross) | Все пакеты пересобираются при изменении |
| Conan-нативный механизм | |

### Уровень 2 — Conan profile `[buildenv]` секция

Env-var'ы доступные в build phase:

```ini
[buildenv]
# Перебивает компиляторы (например для cross-build)
CC=/opt/x64-native-gcc/bin/gcc
CXX=/opt/x64-native-gcc/bin/g++
LD=ld
AR=ar

# Кастомные флаги через env
CFLAGS=-O2 -g1
CXXFLAGS=-O2 -g1 -Wno-deprecated-declarations
LDFLAGS=-Wl,--as-needed

# Variables читаемые CMake через ENV{...}
GRPC_RUNTIME_FLAG=production
```

| Плюсы | Минусы |
|---|---|
| Хорошо для cross-build (CC/CXX override) | Не записывается в package_id — не влияет на кэширование |
| Перебивает leak'и compiler-prefix'ов из host профайла | Env-var'ы могут потеряться между фазами |

Наш `profiles/lin-gcc84-x86_64` имеет явный `[buildenv]` чтобы перебить leak `arm-linux-gnueabihf-*` из host профайла при ARM cross.

### Уровень 3 — Per-package `[options]`

В профайле или через CLI:

```ini
[options]
grpc/*:codegen=False               # не собирать grpc_cpp_plugin
grpc/*:secure=False                # без openssl
grpc/*:use_systemd=False
abseil/*:shared=False              # always static (legacy compat)
*/*:shared=False                   # global default
```

Или через CLI:
```bash
conan create grpc/ --version=1.60.1 \
    -o "grpc/*:codegen=False" \
    -o "grpc/*:use_systemd=False" \
    -o "*/*:shared=False"
```

| Плюсы | Минусы |
|---|---|
| Conan-нативное, идёт в package_id | Опция должна быть определена в `conanfile.py::options = {…}` |
| Один hash per option combo (кэш работает) | Нельзя добавить произвольную CMake-define |

### Уровень 4 — `tools.cmake.cmaketoolchain:extra_variables`

CMake-cache переменные, попадают в `conan_toolchain.cmake` как `set(<var> <val> CACHE INTERNAL "")` до `project()`:

В профайле:
```ini
[conf]
tools.cmake.cmaketoolchain:extra_variables*={
    "BUILD_TESTING": "OFF",
    "gRPC_USE_SYSTEMD": "OFF",
    "gRPC_BUILD_TESTS": "OFF",
    "gRPC_INSTALL": "ON",
    "CMAKE_POSITION_INDEPENDENT_CODE": "ON",
    "ABSL_PROPAGATE_CXX_STD": "ON"
}
```

Через CLI:
```bash
conan create grpc/ ... \
    -c 'tools.cmake.cmaketoolchain:extra_variables*={"gRPC_USE_SYSTEMD": "OFF"}'
```

**Это правильный способ передать CMake-define когда нет соответствующей Conan-option.**

### Уровень 5 — Patches и `replace_in_file()` в `conanfile.py`

Если нужно поменять что-то **не настраиваемое** через CMake-cache (условный `if()`, hardcoded `set()` в CMakeLists.txt):

#### 5a) Через патч (предпочтительно — аудитируется):

В `grpc/conandata.yml`:
```yaml
patches:
  "1.60.1":
    - patch_file: "patches/force-no-systemd.patch"
      patch_description: "Force GRPC_USE_SYSTEMD=OFF unconditionally"
      patch_type: "conan"
```

В файле `patches/force-no-systemd.patch`:
```diff
--- a/CMakeLists.txt
+++ b/CMakeLists.txt
@@ -123,7 +123,7 @@ option(gRPC_USE_SYSTEMD "Use libsystemd" ON)
-option(gRPC_USE_SYSTEMD "Use libsystemd" ON)
+option(gRPC_USE_SYSTEMD "Use libsystemd" OFF)
```

#### 5b) Динамически в `_patch_sources()`:

```python
def _patch_sources(self):
    if not self.options.use_systemd:
        replace_in_file(self,
            os.path.join(self.source_folder, "CMakeLists.txt"),
            'option(gRPC_USE_SYSTEMD "Use libsystemd" ON)',
            'option(gRPC_USE_SYSTEMD "Use libsystemd" OFF)'
        )
```

| Плюсы | Минусы |
|---|---|
| Аудитируется через `git log <pkg>/patches/` | Нужно обновлять при upgrade upstream |
| Гарантированно применяется | Падает на conflict при изменениях upstream — это **сигнал** что надо ревью |

### Уровень 6 — Env-var'ы для grpc-специфичных переменных

grpc CMakeLists.txt читает некоторые env прямо:

```bash
GRPC_BUILD_DISABLE_FEATURE=tracing  ./test-astra/run_grpc_1601_upstream.sh
GRPC_DEBUG_VERBOSITY=DEBUG          ./test-astra/run_grpc_1601_upstream.sh
```

Документировано в upstream grpc README.

### Уровень 7 — Прямо в CLI при ручной сборке

```bash
conan create grpc/ --version=1.60.1 \
    -pr=astra-gcc -s build_type=Release --build=missing --no-remote \
    -o "grpc/*:codegen=False" \
    -c 'tools.cmake.cmaketoolchain:extra_variables*={"gRPC_USE_SYSTEMD": "OFF", "BUILD_TESTING": "OFF"}' \
    -c 'tools.build:cflags=["-O2", "-DGRPC_PRODUCTION=1"]'
```

| Плюсы | Минусы |
|---|---|
| Не нужно менять файлы — для разовой задачи | Не воспроизводится (другой ничего не знает) |
| Видно в командной истории | Не идёт в git |

### Резюме: рекомендуемое распределение

| Что нужно | Где задавать |
|---|---|
| Архитектура / cppstd / компилятор | Уровень 1 — `[settings]` профайла |
| Cross-build CC/CXX, leak'и tools | Уровень 2 — `[buildenv]` профайла |
| static vs shared | Уровень 3 — `[options]` (per-package или `*/*`) |
| Включить/выключить grpc-фичу через CMake-define | Уровень 4 — `extra_variables` |
| Hardcoded поведение в grpc CMakeLists | Уровень 5 — patches |
| Одноразовый эксперимент | Уровень 7 — CLI `-c` или `-o` |

---

## 11. Связанные документы

- [USAGE.md](USAGE.md) — hands-on tutorial Conan + наши `.nupkg`
- [CONANFILE-ANATOMY.md](CONANFILE-ANATOMY.md) — структура каждого из 9 наших conanfile.py
- [MIGRATION-PLAYBOOK.md](MIGRATION-PLAYBOOK.md) — полная методология миграции + 21 разобранный кейс
- [DOWNSTREAM-MIGRATION.md](DOWNSTREAM-MIGRATION.md) — что менять у потребителей (el_conf, grpc_sdk, sura)
- [INFRASTRUCTURE.md](INFRASTRUCTURE.md) — инфраструктура (Bitbucket, TC, Docker, ProGet, dev-VMs)
