#pragma once

/**************************************************************************************************
 * BSP configuration
 **************************************************************************************************/
// Sentinel UI is LVGL; av_render borrows the same panel during a call.
#if !defined(BSP_CONFIG_NO_GRAPHIC_LIB)
#define BSP_CONFIG_NO_GRAPHIC_LIB (0)
#endif
