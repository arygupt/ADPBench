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

    localparam BEATS_PER_ROW = COLS / LANES;
    localparam TOTAL_A_BEATS = ROWS * BEATS_PER_ROW;

    localparam IDLE    = 2'd0;
    localparam COMPUTE = 2'd1;
    localparam OUTPUT  = 2'd2;

    reg [1:0]  state;
    reg [5:0]  beat_count;
    reg [3:0]  out_count;
    reg signed [ACC_W-1:0] acc [0:ROWS-1];
    reg signed [DATA_W-1:0] x_buf [0:COLS-1];
    reg        x_buf_valid;

    wire [3:0] in_row;
    wire [5:0] in_col_base;

    assign in_row = beat_count[5:2];
    assign in_col_base = {4'd0, beat_count[1:0]} * LANES;

    wire signed [ACC_W-1:0] lane_sum;
    reg signed [15:0] prod [0:LANES-1];
    reg signed [ACC_W-1:0] lane_acc;

    integer i;
    always @(*) begin
        lane_acc = acc[in_row];
        for (i = 0; i < LANES; i = i + 1) begin
            prod[i] = $signed(in_a_flat[i*DATA_W +: DATA_W]) * $signed(x_buf[in_col_base + i[5:0]]);
            lane_acc = lane_acc + {{(ACC_W-16){prod[i][15]}}, prod[i]};
        end
    end

    assign out_valid = (state == OUTPUT);
    assign out_c = acc[out_count];

    assign in_x_flat_ready = (state == IDLE) && !x_buf_valid;
    assign in_a_flat_ready = (state == COMPUTE);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= IDLE;
            beat_count <= 6'd0;
            out_count <= 4'd0;
            x_buf_valid <= 1'b0;
            for (i = 0; i < ROWS; i = i + 1)
                acc[i] <= {ACC_W{1'b0}};
        end else begin
            case (state)
                IDLE: begin
                    if (in_x_flat_valid && in_x_flat_ready) begin
                        for (i = 0; i < LANES; i = i + 1)
                            x_buf[{4'd0, beat_count[1:0]} * LANES + i] <= in_x_flat[i*DATA_W +: DATA_W];
                        if (beat_count[1:0] == 2'd3) begin
                            x_buf_valid <= 1'b1;
                            beat_count <= 6'd0;
                            state <= COMPUTE;
                        end else begin
                            beat_count <= beat_count + 6'd1;
                        end
                    end
                end
                COMPUTE: begin
                    if (in_a_flat_valid) begin
                        acc[in_row] <= lane_acc;
                        beat_count <= beat_count + 6'd1;
                        if (beat_count == TOTAL_A_BEATS[5:0] - 6'd1) begin
                            state <= OUTPUT;
                            out_count <= 4'd0;
                            beat_count <= 6'd0;
                        end
                    end
                end
                OUTPUT: begin
                    if (out_ready) begin
                        acc[out_count] <= {ACC_W{1'b0}};
                        if (out_count == ROWS[3:0] - 4'd1) begin
                            state <= IDLE;
                            out_count <= 4'd0;
                            x_buf_valid <= 1'b0;
                        end else begin
                            out_count <= out_count + 4'd1;
                        end
                    end
                end
                default: state <= IDLE;
            endcase
        end
    end

endmodule
