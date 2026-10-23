// {{NAME}}: an empty BepInEx 5 plugin (Rocsy REA). The Master Agent fills it in from PLAN.md.
// Harmony is available for patching game methods: new Harmony("{{GUID}}").PatchAll();
using BepInEx;
using HarmonyLib;
using UnityEngine;

namespace {{CLASS}}
{
    [BepInPlugin("{{GUID}}", "{{NAME}}", "1.0.0")]
    public class Plugin : BaseUnityPlugin
    {
        void Awake()
        {
            new Harmony("{{GUID}}").PatchAll();
            Logger.LogInfo("{{NAME}} loaded");
        }
    }
}
