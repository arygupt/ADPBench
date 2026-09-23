// GEMV accelerator: y = A @ x
// A: ROWS x COLS, int8, row-major; x: COLS int8; y: ROWS int32.
// Streams are independent valid/ready. Two back-to-back transactions.
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

    localparam NX_BEATS = COLS / LANES;         // 4
    localparam NA_BEATS = ROWS * COLS / LANES;  // 64
    localparam BPR      = COLS / LANES;         // 4 beats per row
    localparam SUM_W    = 2*DATA_W + $clog2(LANES) + 1;

    localparam ST_X = 2'd0;
    localparam ST_A = 2'd1;
    localparam ST_F = 2'd2;

    reg [1:0] state;
    reg [1:0] x_cnt;
    reg [5:0] a_cnt;
    reg signed [ACC_W-1:0] acc;
    reg signed [ACC_W-1:0] out_c_r;
    reg out_valid_r;

    // x storage: four 128-bit groups (16 int8 each)
    reg [LANES*DATA_W-1:0] xg0, xg1, xg2, xg3;

    // select the x group for the current A beat
    reg [LANES*DATA_W-1:0] x_word;
    always @(*) begin
        case (a_cnt[1:0])
            2'd0: x_word = xg0;
            2'd1: x_word = xg1;
            2'd2: x_word = xg2;
            2'd3: x_word = xg3;
            default: x_word = xg0;
        endcase
    end

    // element extraction and multiplication
    wire signed [DATA_W-1:0]   a_elem [0:LANES-1];
    wire signed [DATA_W-1:0]   x_elem [0:LANES-1];
    wire signed [2*DATA_W-1:0] prod   [0:LANES-1];

    genvar gi;
    generate
        for (gi = 0; gi < LANES; gi = gi + 1) begin : gen_elems
            assign a_elem[gi] = in_a_flat[gi*DATA_W +: DATA_W];
            assign x_elem[gi] = x_word[gi*DATA_W +: DATA_W];
            assign prod[gi]   = a_elem[gi] * x_elem[gi];
        end
    endgenerate

    // sum of products for one beat
    reg signed [SUM_W-1:0] dot;
    integer m;
    always @(*) begin
        dot = {SUM_W{1'b0}};
        for (m = 0; m < LANES; m = m + 1) begin
            dot = dot + prod[m];
        end
    end

    wire signed [ACC_W-1:0] acc_next = acc + dot;

    assign in_x_flat_ready = (state == ST_X);
    assign in_a_flat_ready = (state == ST_A) && (!out_valid_r || out_ready);
    assign out_valid      = out_valid_r;
    assign out_c          = out_c_r;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state      <= ST_X;
            x_cnt      <= 2'd0;
            a_cnt      <= 6'd0;
            acc        <= 0;
            out_c_r    <= 0;
            out_valid_r<= 1'b0;
            xg0        <= 0;
            xg1        <= 0;
            xg2        <= 0;
            xg3        <= 0;
        end else begin
            // clear out_valid when the output is accepted
            if (out_valid_r && out_ready) begin
                out_valid_r <= 1'b0;
            end

            case (state)
                ST_X: begin
                    if (in_x_flat_valid && in_x_flat_ready) begin
                        case (x_cnt)
                            2'd0: xg0 <= in_x_flat;
                            2'd1: xg1 <= in_x_flat;
                            2'd2: xg2 <= in_x_flat;
                            2'd3: xg3 <= in_x_flat;
                        endcase
                        if (x_cnt == NX_BEATS-1) begin
                            x_cnt   <= 2'd0;
                            a_cnt   <= 6'd0;
                            acc     <= 0;
                            state   <= ST_A;
                        end else begin
                            x_cnt   <= x_cnt + 1'b1;
                        end
                    end
                end

                ST_A: begin
                    if (in_a_flat_valid && in_a_flat_ready) begin
                        if (a_cnt[1:0] == 2'd3) begin
                            // last beat of a row: output accumulated sum
                            out_c_r    <= acc_next;
                            out_valid_r<= 1'b1;
                            acc        <= 0;
                        end else begin
                            acc        <= acc_next;
                        end
                        if (a_cnt == NA_BEATS-1) begin
                            state      <= ST_F;
                        end else begin
                            a_cnt      <= a_cnt + 1'b1;
                        end
                    end
                end

                ST_F: begin
                    if (!out_valid_r || out_ready) begin
                        state <= ST_X;
                        x_cnt <= 2'd0;
                    end
                end

                default: begin
                    state <= ST_X;
                end
            endcase
        end
    end

endmodule
