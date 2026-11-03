using System;
using System.Net.Http;
using System.Threading;
using System.Threading.Tasks;
using NuGet.Common;
using NuGet.Configuration;
using NuGet.Protocol;
using NuGet.Protocol.Core.Types;

class Program
{
    // Режимы: http <url> | plain <root|-> | creds <root|->. plain повторяет код из IN-766.
    static async Task<int> Main(string[] args)
    {
        var mode = args[0];
        if (mode == "http")
            return await Http(new Uri(args[1]));
        var root = args.Length > 1 && args[1] != "-" ? args[1] : null;
        if (mode == "creds")
            NuGet.Credentials.DefaultCredentialServiceUtility.SetupDefaultCredentialService(NullLogger.Instance, nonInteractive: true);

        var settings = Settings.LoadDefaultSettings(root: root);
        foreach (var path in settings.GetConfigFilePaths())
            Console.WriteLine("  config: " + path);
        var provider = new SourceRepositoryProvider(new PackageSourceProvider(settings), Repository.Provider.GetCoreV3());
        var failed = 0;
        foreach (var repository in provider.GetRepositories())
        {
            var source = repository.PackageSource;
            Console.WriteLine($"  source {source.Name} {source.Source} credentials={source.Credentials != null}");
            try
            {
                var resource = await repository.GetResourceAsync<DependencyInfoResource>(CancellationToken.None);
                Console.WriteLine("    OK " + resource?.GetType().Name);
            }
            catch (Exception error)
            {
                failed++;
                for (var e = error; e != null; e = e.InnerException)
                    Console.WriteLine($"    FAIL {e.GetType().FullName}: {e.Message}");
            }
        }
        return failed == 0 ? 0 : 1;
    }

    static async Task<int> Http(Uri url)
    {
        var proxy = HttpClient.DefaultProxy;
        Console.WriteLine("  proxy: " + (proxy.IsBypassed(url) ? "нет" : proxy.GetProxy(url)?.ToString() ?? "нет"));
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(30) };
            var response = await client.GetAsync(url);
            Console.WriteLine($"  HTTP {(int)response.StatusCode} {response.ReasonPhrase}");
            return response.IsSuccessStatusCode ? 0 : 1;
        }
        catch (Exception error)
        {
            for (var e = error; e != null; e = e.InnerException)
                Console.WriteLine($"  FAIL {e.GetType().FullName}: {e.Message}");
            return 1;
        }
    }
}
