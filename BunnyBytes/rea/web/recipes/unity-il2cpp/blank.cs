// {{NAME}}: an empty BepInEx 6 IL2CPP plugin (Rocsy REA). The Master Agent fills it in from PLAN.md.
// Harmony works on IL2CPP methods through Il2CppInterop: new Harmony("{{GUID}}").PatchAll();
using BepInEx;
using BepInEx.Unity.IL2CPP;
using HarmonyLib;

namespace {{CLASS}}
{
    [BepInPlugin("{{GUID}}", "{{NAME}}", "1.0.0")]
    public class Plugin : BasePlugin
    {
        public override void Load()
        {
            new Harmony("{{GUID}}").PatchAll();
            Log.LogInfo("{{NAME}} loaded");
        }
    }
}
