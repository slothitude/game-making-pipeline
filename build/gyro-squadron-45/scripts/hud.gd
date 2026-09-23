class_name Hud
extends CanvasLayer
## Act-1 HUD: score, lives, weapon tier pips, charge meter bar, bomb count —
## anchored top, chunky outlined text. Also hosts the GAME OVER overlay with
## the RETRY prompt and the stage-clear tally. Built in code, no theme files.

var score_label: Label
var lives_label: Label
var tier_label: Label
var bombs_label: Label
var charge_bar: ChargeBar
var overlay: ColorRect
var overlay_title: Label
var overlay_hint: Label
var banner_label: Label
var _banner_text := ""

var _state := {
	"score": 0,
	"lives": Feel.PLAYER_LIVES,
	"tier": Feel.WEAPON_TIER_MIN,
	"charge": 0,
	"needed": Feel.SUPER_SHOT_CHARGE_KILLS,
	"bombs": Feel.BOMB_START_COUNT,
}


func _ready() -> void:
	score_label = _make_label(Feel.HUD_FONT_SIZE + 4)
	score_label.position = Vector2(150.0, Feel.HUD_MARGIN_PX)
	score_label.size = Vector2(240.0, 40.0)
	score_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER

	lives_label = _make_label(Feel.HUD_FONT_SIZE)
	lives_label.position = Vector2(Feel.HUD_MARGIN_PX, Feel.HUD_MARGIN_PX)
	lives_label.size = Vector2(140.0, 36.0)

	tier_label = _make_label(Feel.HUD_FONT_SIZE)
	tier_label.position = Vector2(320.0, Feel.HUD_MARGIN_PX)
	tier_label.size = Vector2(212.0, 36.0)
	tier_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_RIGHT

	bombs_label = _make_label(Feel.HUD_FONT_SIZE)
	bombs_label.position = Vector2(Feel.HUD_MARGIN_PX, Feel.HUD_MARGIN_PX + 38.0)
	bombs_label.size = Vector2(160.0, 32.0)

	charge_bar = ChargeBar.new()
	charge_bar.position = Vector2(352.0, Feel.HUD_MARGIN_PX + 44.0)
	charge_bar.size = Feel.CHARGE_BAR_SIZE

	overlay = ColorRect.new()
	overlay.color = Color(0.02, 0.03, 0.06, 0.72)
	overlay.set_anchors_preset(Control.PRESET_FULL_RECT)
	overlay.mouse_filter = Control.MOUSE_FILTER_IGNORE  # taps must reach the game
	overlay.visible = false

	overlay_title = _make_label(64)
	overlay_title.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	overlay_title.position = Vector2(0.0, 330.0)
	overlay_title.size = Vector2(540.0, 90.0)

	overlay_hint = _make_label(Feel.HUD_FONT_SIZE)
	overlay_hint.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	overlay_hint.position = Vector2(0.0, 440.0)
	overlay_hint.size = Vector2(540.0, 44.0)

	banner_label = _make_label(46)
	banner_label.horizontal_alignment = HORIZONTAL_ALIGNMENT_CENTER
	banner_label.position = Vector2(0.0, 210.0)
	banner_label.size = Vector2(540.0, 60.0)
	banner_label.visible = false

	add_child(score_label)
	add_child(lives_label)
	add_child(tier_label)
	add_child(bombs_label)
	add_child(charge_bar)
	add_child(banner_label)
	add_child(overlay)
	overlay.add_child(overlay_title)
	overlay.add_child(overlay_hint)
	_refresh()


func _make_label(font_size: int) -> Label:
	var l := Label.new()
	var ls := LabelSettings.new()
	ls.font_size = font_size
	ls.font_color = Feel.HUD_COLOR
	ls.outline_size = Feel.HUD_OUTLINE_SIZE
	ls.outline_color = Feel.HUD_OUTLINE_COLOR
	l.label_settings = ls
	l.mouse_filter = Control.MOUSE_FILTER_IGNORE
	return l


## One call per frame from main: push the stage state into the widgets.
func update_from(stage: Stage) -> void:
	_state.score = stage.score
	_state.lives = maxi(stage.lives, 0)
	_state.bombs = stage.bombs
	if stage.weapon != null:
		_state.tier = stage.weapon.tier
		_state.charge = stage.weapon.kills_charged
		_state.needed = Feel.SUPER_SHOT_CHARGE_KILLS
	_refresh()


func set_charge(kills: int, needed: int) -> void:
	_state.charge = kills
	_state.needed = needed
	_refresh()


func show_game_over() -> void:
	overlay_title.text = "GAME OVER"
	overlay_hint.text = "TAP TO RETRY"
	overlay.visible = true


func show_tally(tally: Dictionary) -> void:
	overlay_title.text = "STAGE CLEAR"
	var lines := "SCORE %d\nKILLS %d" % [int(tally.get("score", 0)), int(tally.get("kills", 0))]
	var bonus := int(tally.get("no_damage_bonus", 0))
	if bonus > 0:
		lines += "\nNO-DAMAGE BONUS %d" % bonus
	overlay_hint.text = lines + "\nTAP TO FLY AGAIN"
	overlay.visible = true


func overlay_visible() -> bool:
	return overlay.visible


## Act-transition card: transient and NON-blocking (play continues under it).
## main drives it from the Stage state each frame.
func set_banner(text: String, shown: bool) -> void:
	if _banner_text == text and banner_label.visible == shown:
		return
	_banner_text = text
	banner_label.text = text
	banner_label.visible = shown


func banner_visible() -> bool:
	return banner_label.visible


func hide_overlay() -> void:
	overlay.visible = false


func _refresh() -> void:
	score_label.text = "%06d" % _state.score
	lives_label.text = "LIVES x%d" % _state.lives
	tier_label.text = _pips(_state.tier)
	bombs_label.text = "BOMB x%d" % _state.bombs
	charge_bar.set_charge(int(_state.charge), int(_state.needed))


func _pips(tier: int) -> String:
	var out := ""
	for i in Feel.WEAPON_TIERS:
		out += Feel.HUD_TIER_PIP_FULL if i < tier else Feel.HUD_TIER_PIP_EMPTY
	return "PW " + out


class ChargeBar:
	extends Control
	## Charge meter bar: fills per kill; solid + labelled when SUPER is ready.

	var _charge := 0
	var _needed := Feel.SUPER_SHOT_CHARGE_KILLS

	func set_charge(kills: int, needed: int) -> void:
		_charge = kills
		_needed = maxi(needed, 1)
		queue_redraw()

	func _draw() -> void:
		var full := _charge >= _needed
		var bg := Rect2(Vector2.ZERO, size)
		draw_rect(bg, Feel.CHARGE_EMPTY_COLOR)
		var frac := clampf(float(_charge) / float(_needed), 0.0, 1.0)
		if frac > 0.0:
			draw_rect(Rect2(Vector2.ZERO, Vector2(size.x * frac, size.y)), Feel.CHARGE_FULL_COLOR)
		draw_rect(bg, Feel.HUD_OUTLINE_COLOR, false, 3.0)
		if full:
			var font := ThemeDB.fallback_font
			draw_string(font, Vector2(size.x + 8.0, size.y - 2.0), "SUPER!",
					HORIZONTAL_ALIGNMENT_LEFT, 90.0, 20.0, Feel.CHARGE_FULL_COLOR)
