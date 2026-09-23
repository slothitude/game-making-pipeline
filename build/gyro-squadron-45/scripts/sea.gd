class_name SeaScroll
extends Node2D
## Milestone 1 scrolling sea. Uses assets/generated/tile_sea.png when it has
## landed (tiled, scrolled via region offset); code-drawn bands otherwise.
## Never blocks on art.

const TILE_PATH := "res://assets/generated/tile_sea.png"
const SEA_COLOR_A := Color(0.075, 0.23, 0.36)
const SEA_COLOR_B := Color(0.11, 0.30, 0.45)

var _offset := 0.0
var _sprite: Sprite2D
var _use_texture := false


func _ready() -> void:
	if ResourceLoader.exists(TILE_PATH):
		var tex: Texture2D = load(TILE_PATH)
		if tex != null:
			_use_texture = true
			_sprite = Sprite2D.new()
			_sprite.texture = tex
			_sprite.centered = false
			_sprite.region_enabled = true
			_sprite.texture_repeat = CanvasItem.TEXTURE_REPEAT_ENABLED
			add_child(_sprite)


func _process(delta: float) -> void:
	_offset -= Feel.SCROLL_SPEED_TEST * delta  # content moves down = flying on
	if _use_texture and _sprite != null:
		var view := get_viewport_rect().size
		_sprite.region_rect = Rect2(0.0, _offset, view.x, view.y)
	else:
		queue_redraw()


func _draw() -> void:
	if _use_texture:
		return
	var view := get_viewport_rect().size
	var band := Feel.SEA_BAND_PX
	var y := -fposmod(_offset, band * 2.0)
	while y < view.y:
		draw_rect(Rect2(0.0, y, view.x, band), SEA_COLOR_A)
		draw_rect(Rect2(0.0, y + band, view.x, band), SEA_COLOR_B)
		y += band * 2.0
