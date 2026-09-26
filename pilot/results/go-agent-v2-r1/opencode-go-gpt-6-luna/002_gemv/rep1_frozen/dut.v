module dut #(
    parameter ROWS = 16,
    parameter COLS = 64,
    parameter LANES = 16,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_x_flat,
    input  wire                    in_x_flat_valid,
    output wire                    in_x_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);
    localparam integer BEATS_PER_ROW = COLS / LANES;
    localparam integer A_BEATS_PER_TX = ROWS * BEATS_PER_ROW;
    localparam integer X_BEATS_PER_TX = COLS / LANES;
    localparam integer TOTAL_A_BEATS = 2 * A_BEATS_PER_TX;
    localparam integer TOTAL_X_BEATS = 2 * X_BEATS_PER_TX;
    localparam integer FIFO_DEPTH = 2 * ROWS;
    localparam integer A_CNT_W = (TOTAL_A_BEATS < 2) ? 1 : $clog2(TOTAL_A_BEATS+1);
    localparam integer X_CNT_W = (TOTAL_X_BEATS < 2) ? 1 : $clog2(TOTAL_X_BEATS+1);
    localparam integer FIFO_CNT_W = $clog2(FIFO_DEPTH+1);
    localparam integer FIFO_PTR_W = (FIFO_DEPTH < 2) ? 1 : $clog2(FIFO_DEPTH);

    reg [A_CNT_W-1:0] a_beat_count;
    reg [X_CNT_W-1:0] x_beat_count;
    reg [DATA_W-1:0] x_mem [0:(2*COLS)-1];
    reg signed [ACC_W-1:0] row_partial;

    reg signed [ACC_W-1:0] result_fifo [0:FIFO_DEPTH-1];
    reg [FIFO_PTR_W-1:0] wr_ptr;
    reg [FIFO_PTR_W-1:0] rd_ptr;
    reg [FIFO_CNT_W-1:0] fifo_count;

    wire fifo_pop = out_valid && out_ready;
    wire a_is_last_row_beat = ((a_beat_count % BEATS_PER_ROW) == (BEATS_PER_ROW-1));
    wire fifo_space = (fifo_count < FIFO_DEPTH) || fifo_pop;
    wire x_for_current_a_ready = (x_beat_count >= ((a_beat_count / A_BEATS_PER_TX) + 1) * X_BEATS_PER_TX);

    assign in_x_flat_ready = rst_n && (x_beat_count < TOTAL_X_BEATS);
    assign in_a_flat_ready = rst_n && (a_beat_count < TOTAL_A_BEATS) &&
                             x_for_current_a_ready &&
                             (!a_is_last_row_beat || fifo_space);
    assign out_valid = (fifo_count != 0);
    assign out_c = result_fifo[rd_ptr];

    integer lane;
    integer x_index;
    reg signed [DATA_W-1:0] a_value;
    reg signed [DATA_W-1:0] x_value;
    reg signed [2*DATA_W-1:0] product;
    reg signed [ACC_W-1:0] beat_sum;

    always @* begin
        beat_sum = {ACC_W{1'b0}};
        x_index = 0;
        a_value = {DATA_W{1'b0}};
        x_value = {DATA_W{1'b0}};
        product = {(2*DATA_W){1'b0}};
        for (lane = 0; lane < LANES; lane = lane + 1) begin
            x_index = (a_beat_count / A_BEATS_PER_TX) * COLS +
                      (a_beat_count % BEATS_PER_ROW) * LANES + lane;
            a_value = $signed(in_a_flat[lane*DATA_W +: DATA_W]);
            x_value = $signed(x_mem[x_index]);
            product = $signed({{DATA_W{a_value[DATA_W-1]}},a_value}) *
                      $signed({{DATA_W{x_value[DATA_W-1]}},x_value});
            beat_sum = beat_sum + product;
        end
    end

    integer i;
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            a_beat_count <= 0;
            x_beat_count <= 0;
            row_partial <= 0;
            wr_ptr <= 0;
            rd_ptr <= 0;
            fifo_count <= 0;
            for (i = 0; i < 2*COLS; i = i + 1)
                x_mem[i] <= 0;
            for (i = 0; i < FIFO_DEPTH; i = i + 1)
                result_fifo[i] <= 0;
        end else begin
            if (in_x_flat_valid && in_x_flat_ready) begin
                for (i = 0; i < LANES; i = i + 1)
                    x_mem[x_beat_count*LANES + i] <= in_x_flat[i*DATA_W +: DATA_W];
                x_beat_count <= x_beat_count + 1'b1;
            end

            if (in_a_flat_valid && in_a_flat_ready) begin
                a_beat_count <= a_beat_count + 1'b1;
                if (a_is_last_row_beat) begin
                    result_fifo[wr_ptr] <= row_partial + beat_sum;
                    row_partial <= 0;
                    if (wr_ptr == FIFO_DEPTH-1)
                        wr_ptr <= 0;
                    else
                        wr_ptr <= wr_ptr + 1'b1;
                end else begin
                    row_partial <= row_partial + beat_sum;
                end
            end

            if (fifo_pop) begin
                if (rd_ptr == FIFO_DEPTH-1)
                    rd_ptr <= 0;
                else
                    rd_ptr <= rd_ptr + 1'b1;
            end

            case ({(in_a_flat_valid && in_a_flat_ready && a_is_last_row_beat), fifo_pop})
                2'b10: fifo_count <= fifo_count + 1'b1;
                2'b01: fifo_count <= fifo_count - 1'b1;
                default: fifo_count <= fifo_count;
            endcase
        end
    end
endmodule
