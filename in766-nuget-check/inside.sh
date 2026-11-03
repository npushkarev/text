#!/usr/bin/env bash
# Выполняется внутри контейнера dotnet. Вход: /src (этот каталог), /repo (необязательно), env FEED, NUGET_VERSION, PROGET_USER, PROGET_PASSWORD.
set -u
step() { echo; echo "######## $*"; }

step "окружение"
dotnet --version
id; echo "HOME=$HOME"
env | grep -iE 'proxy|nuget|dotnet_' | sed -E 's/((PASSWORD|TOKEN|CREDENTIALS)[^=]*=).*/\1***/I' | sort

step "конфиги NuGet, которые видит LoadDefaultSettings(root: null)"
for f in "$HOME/.nuget/NuGet/NuGet.Config" "$HOME/.nuget/NuGet/config/"*.config /etc/opt/NuGet/Config/*.config ${NUGET_COMMON_APPLICATION_DATA:+$NUGET_COMMON_APPLICATION_DATA/NuGet/Config/*.config}; do
    [ -f "$f" ] && { echo "== $f"; grep -viE 'password' "$f"; }
done
echo "(строки с password скрыты)"

if [ -d /repo ]; then
    step "nuget.config в репозитории /repo"
    find /repo -maxdepth 4 -iname 'nuget.config' -not -path '*/bin/*' -not -path '*/obj/*' | while read -r f; do
        echo "== $f"; grep -viE 'password' "$f"
    done
fi

step "dotnet nuget list source (как видит CLI из $HOME)"
(cd /tmp && dotnet nuget list source --format detailed 2>&1)
[ -d /repo ] && { step "dotnet nuget list source из /repo"; (cd /repo && dotnet nuget list source --format detailed 2>&1); }

W=/tmp/in766; rm -rf "$W"; mkdir -p "$W/app"; cd "$W/app"
TFM="net$(dotnet --version | cut -d. -f1).0"
CREDS=""
if [ -n "${PROGET_USER:-}" ]; then
    CREDS="<packageSourceCredentials><feed><add key=\"Username\" value=\"$PROGET_USER\" /><add key=\"ClearTextPassword\" value=\"$PROGET_PASSWORD\" /></feed></packageSourceCredentials>"
fi
cat > nuget.config <<XML
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <packageSources><clear /><add key="feed" value="$FEED" allowInsecureConnections="true" /></packageSources>
  $CREDS
</configuration>
XML
cat > check.csproj <<XML
<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><OutputType>Exe</OutputType><TargetFramework>$TFM</TargetFramework><Nullable>disable</Nullable><LangVersion>latest</LangVersion></PropertyGroup>
  <ItemGroup>
    <PackageReference Include="NuGet.Protocol" Version="$NUGET_VERSION" />
    <PackageReference Include="NuGet.Commands" Version="$NUGET_VERSION" />
  </ItemGroup>
</Project>
XML
cp /src/Program.cs .

step "1. dotnet restore через CLI, фид $FEED (NuGet.Protocol $NUGET_VERSION, $TFM)"
if ! dotnet restore --nologo -v minimal 2>&1; then
    echo "[ИТОГ] dotnet restore не смог взять пакеты из $FEED. Сам CLI тоже не работает с этим адресом."
    echo "       Проверьте FEED (V3: .../nuget/<фид>/v3/index.json), учётку PROGET_USER/PROGET_PASSWORD, NUGET_VERSION."
    exit 2
fi
dotnet build --nologo -v quiet -c Release -o "$W/bin" > "$W/build.log" 2>&1 \
    || { tail -30 "$W/build.log"; echo "[ИТОГ] проверочное приложение не собралось"; exit 3; }
RUN="dotnet $W/bin/check.dll"
cd /tmp

step "2. HttpClient напрямую: $FEED"
$RUN http "$FEED"; r_http=$?
step "3. NuGet.Protocol, root=null, как в IN-766"
$RUN plain -; r_plain=$?
step "4. NuGet.Protocol, root=null + DefaultCredentialServiceUtility (провайдеры учёток, как у CLI)"
$RUN creds -; r_creds=$?
step "5. NuGet.Protocol, явный конфиг с фидом $FEED"
$RUN plain "$W/app"; r_explicit=$?
r_repo=-
if [ -d /repo ]; then
    step "6. NuGet.Protocol, root=/repo (конфиг из репозитория)"
    $RUN plain /repo; r_repo=$?
fi

step "ИТОГ (0 = работает)"
echo "http=$r_http  root_null=$r_plain  root_null+creds=$r_creds  explicit=$r_explicit  repo=$r_repo"
[ "$r_plain" != 0 ] && [ "$r_creds" = 0 ] && echo "=> Не хватает провайдера учётных данных. Исправление: вызвать DefaultCredentialServiceUtility.SetupDefaultCredentialService в тестах."
[ "$r_plain" != 0 ] && [ "$r_repo" = 0 ] && echo "=> Не прочитан nuget.config репозитория. Исправление: LoadDefaultSettings(root: <папка решения>)."
[ "$r_plain" != 0 ] && [ "$r_explicit" = 0 ] && [ "$r_creds" != 0 ] && echo "=> С явным фидом работает: смотрите источники в шаге 3, там не тот URL или нет учётки."
[ "$r_http" != 0 ] && echo "=> Сам HTTP до фида не проходит: прокси, DNS или учётка. См. шаг 1."
exit 0
