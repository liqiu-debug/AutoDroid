import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import {
    TitleComponent,
    TooltipComponent,
    LegendComponent,
    GridComponent,
    MarkPointComponent,
    MarkLineComponent,
    ToolboxComponent,
    DataZoomComponent,
} from 'echarts/components'

use([
    CanvasRenderer,
    LineChart,
    TitleComponent,
    TooltipComponent,
    LegendComponent,
    GridComponent,
    MarkPointComponent,
    MarkLineComponent,
    ToolboxComponent,
    DataZoomComponent,
])
