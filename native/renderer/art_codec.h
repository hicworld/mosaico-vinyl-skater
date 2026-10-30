#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
bool art_decode(const uint8_t *src,size_t packed,uint8_t *dst,size_t raw);
bool art_overlay(const uint16_t *runs,size_t words,uint16_t *pixels,size_t count);
bool art_overlay_indexed(const uint8_t *indices,size_t bytes,const uint16_t *palette,size_t palette_count,uint16_t *pixels,size_t count);
