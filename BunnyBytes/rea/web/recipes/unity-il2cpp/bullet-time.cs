// {{NAME}}: bullet time for a Unity IL2CPP game (Rocsy REA recipe, BepInEx 6).
// IL2CPP games need managed MonoBehaviours registered with Il2CppInterop before use; the plugin
// creates one hidden GameObject that carries the behaviour. Keys/speed: BepInEx/config/{{GUID}}.cfg.
using System;
using BepInEx;
using BepInEx.Configuration;
using BepInEx.Unity.IL2CPP;
using Il2CppInterop.Runtime.Injection;
using UnityEngine;

namespace {{CLASS}}
{
    [BepInPlugin("{{GUID}}", "{{NAME}}", "1.0.0")]
    public class Plugin : BasePlugin
    {
        internal static ConfigEntry<KeyCode> ToggleKey;
        internal static ConfigEntry<float> SlowScale;
        internal static ConfigEntry<float> BlendSeconds;
        internal static BepInEx.Logging.ManualLogSource Log;

        public override void Load()
        {
            Log = base.Log;
            ToggleKey = Config.Bind("Controls", "Toggle", KeyCode.T, "Turns bullet time on and off");
            SlowScale = Config.Bind("Bullet time", "TimeScale", 0.3f, "Game speed while active (1 = normal)");
            BlendSeconds = Config.Bind("Bullet time", "BlendSeconds", 0.25f, "Real seconds to ease in and out");
            ClassInjector.RegisterTypeInIl2Cpp<BulletTime>();
            var host = new GameObject("{{CLASS}}");
            UnityEngine.Object.DontDestroyOnLoad(host);
            host.hideFlags = HideFlags.HideAndDontSave;
            host.AddComponent<BulletTime>();
            Log.LogInfo($"{{NAME}} loaded: press {ToggleKey.Value} for bullet time");
        }
    }

    public class BulletTime : MonoBehaviour
    {
        public BulletTime(IntPtr ptr) : base(ptr) { }
        float baseFixedDelta = 0.02f;
        float current = 1f;
        bool active;

        void Start() { baseFixedDelta = Time.fixedDeltaTime; }

        bool Pressed()
        {
            // Games built with only the new Input System throw on the legacy API.
            try { return Input.GetKeyDown(Plugin.ToggleKey.Value); } catch { return false; }
        }

        void Update()
        {
            if (Pressed())
            {
                active = !active;
                Plugin.Log.LogInfo(active ? "Bullet time on" : "Bullet time off");
            }
            if (Time.timeScale == 0f) return; // paused: leave menus alone
            float target = active ? Plugin.SlowScale.Value : 1f;
            float step = Plugin.BlendSeconds.Value <= 0f ? 1f : Time.unscaledDeltaTime / Plugin.BlendSeconds.Value;
            current = Mathf.MoveTowards(current, target, step);
            if (!active && Mathf.Approximately(current, 1f) && Mathf.Approximately(Time.timeScale, 1f)) return;
            Time.timeScale = current;
            Time.fixedDeltaTime = baseFixedDelta * current;
        }
    }
}
