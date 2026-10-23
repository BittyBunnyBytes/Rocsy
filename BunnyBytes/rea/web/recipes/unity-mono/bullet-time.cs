// {{NAME}}: bullet time for any Unity game (Rocsy REA recipe).
// Toggle slows the whole game down; physics keep their smoothness because fixedDeltaTime scales
// with timeScale. Keys and speed are in BepInEx/config/{{GUID}}.cfg after the first launch.
// Next steps the Master Agent can take after reading the game's code: keep the player at full
// speed, slow audio pitch too, hook the game's own pause so menus aren't fought over.
using BepInEx;
using BepInEx.Configuration;
using UnityEngine;

namespace {{CLASS}}
{
    [BepInPlugin("{{GUID}}", "{{NAME}}", "1.0.0")]
    public class Plugin : BaseUnityPlugin
    {
        ConfigEntry<KeyboardShortcut> toggleKey;
        ConfigEntry<float> slowScale;
        ConfigEntry<float> blendSeconds;
        float baseFixedDelta = 0.02f;
        float current = 1f;
        bool active;

        void Awake()
        {
            toggleKey = Config.Bind("Controls", "Toggle", new KeyboardShortcut(KeyCode.T), "Turns bullet time on and off");
            slowScale = Config.Bind("Bullet time", "TimeScale", 0.3f,
                new ConfigDescription("Game speed while active (1 = normal)", new AcceptableValueRange<float>(0.05f, 1f)));
            blendSeconds = Config.Bind("Bullet time", "BlendSeconds", 0.25f, "Real seconds to ease in and out");
            baseFixedDelta = Time.fixedDeltaTime;
            Logger.LogInfo($"{{NAME}} loaded: press {toggleKey.Value} for bullet time");
        }

        void Update()
        {
            if (toggleKey.Value.IsDown())
            {
                active = !active;
                Logger.LogInfo(active ? "Bullet time on" : "Bullet time off");
            }
            // A paused game (timeScale 0) is left alone, so pause menus keep working.
            if (Time.timeScale == 0f) return;
            float target = active ? slowScale.Value : 1f;
            float step = blendSeconds.Value <= 0f ? 1f : Time.unscaledDeltaTime / blendSeconds.Value;
            current = Mathf.MoveTowards(current, target, step);
            if (!active && Mathf.Approximately(current, 1f) && Mathf.Approximately(Time.timeScale, 1f)) return;
            Time.timeScale = current;
            Time.fixedDeltaTime = baseFixedDelta * current;
        }

        void OnDisable()
        {
            Time.timeScale = 1f;
            Time.fixedDeltaTime = baseFixedDelta;
        }

        void OnGUI()
        {
            if (!active) return;
            var style = new GUIStyle(GUI.skin.label) { fontSize = 22, fontStyle = FontStyle.Bold };
            style.normal.textColor = new Color(1f, 0.85f, 0.3f);
            GUI.Label(new Rect(24, 20, 400, 40), $"BULLET TIME  x{current:0.00}", style);
        }
    }
}
