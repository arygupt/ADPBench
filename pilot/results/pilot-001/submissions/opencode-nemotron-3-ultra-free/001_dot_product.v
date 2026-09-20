module dut #(
    parameter LEN = 256,
    parameter LANES = 32,
    parameter DATA_W = 8,
    parameter ACC_W = 32
)(
    input  wire                    clk,
    input  wire                    rst_n,
    input  wire [LANES*DATA_W-1:0] in_a_flat,
    input  wire                    in_a_flat_valid,
    output wire                    in_a_flat_ready,
    input  wire [LANES*DATA_W-1:0] in_b_flat,
    input  wire                    in_b_flat_valid,
    output wire                    in_b_flat_ready,
    output wire                    out_valid,
    input  wire                    out_ready,
    output wire signed [ACC_W-1:0] out_c
);

    localparam BEATS = LEN / LANES;
    localparam LPT = 1;
    localparam STEPS = LANES / LPT;

    typedef enum logic [2:0] {WAIT_AB, WAIT_A, WAIT_B, PROC, OUT} state_t;
    state_t state, nxt_state;

    reg [LANES*DATA_W-1:0] a_beat_buf;
    reg [LANES*DATA_W-1:0] b_beat_buf;
    reg a_beat_valid, b_beat_valid;
    reg [$clog2(STEPS):0] step_cnt;
    reg [3:0] beat_cnt;
    reg signed [ACC_W-1:0] acc;
    reg signed [ACC_W-1:0] out_reg;
    reg out_valid_reg;

    wire signed [DATA_W-1:0] a_lane;
    wire signed [DATA_W-1:0] b_lane;
    wire signed [2*DATA_W-1:0] prod;
    wire signed [ACC_W-1:0] sum_prods;

    assign a_lane = a_beat_buf[DATA_W-1:0];
    assign b_lane = b_beat_buf[DATA_W-1:0];
    assign prod = a_lane * b_lane;
    assign sum_prods = {{(ACC_W-2*DATA_W){prod[2*DATA_W-1]}}, prod};

    assign in_a_flat_ready = (state == WAIT_AB || state == WAIT_A) && !a_beat_valid;
    assign in_b_flat_ready = (state == WAIT_AB || state == WAIT_B) && !b_beat_valid;
    assign out_valid = out_valid_reg;
    assign out_c = out_reg;

    always_comb begin
        nxt_state = state;
        case (state)
            WAIT_AB: if (a_beat_valid && b_beat_valid)       nxt_state = PROC;
                     else if (a_beat_valid)                  nxt_state = WAIT_B;
                     else if (b_beat_valid)                  nxt_state = WAIT_A;
            WAIT_A:  if (a_beat_valid)                       nxt_state = PROC;
            WAIT_B:  if (b_beat_valid)                       nxt_state = PROC;
            PROC:    if (step_cnt == STEPS-1) begin
                         if (beat_cnt == 0)                  nxt_state = OUT;
                         else                                nxt_state = WAIT_AB;
                     end
            OUT:     if (out_ready)                          nxt_state = WAIT_AB;
        endcase
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state         <= WAIT_AB;
            a_beat_valid  <= 0;
            b_beat_valid  <= 0;
            step_cnt      <= 0;
            beat_cnt      <= BEATS - 1;
            acc           <= 0;
            out_valid_reg <= 0;
            out_reg       <= 0;
            a_beat_buf    <= 0;
            b_beat_buf    <= 0;
        end else begin
            state <= nxt_state;

            if (in_a_flat_valid && in_a_flat_ready) begin
                a_beat_buf <= in_a_flat;
                a_beat_valid <= 1;
            end

            if (in_b_flat_valid && in_b_flat_ready) begin
                b_beat_buf <= in_b_flat;
                b_beat_valid <= 1;
            end

            case (state)
                WAIT_AB: begin
                    step_cnt <= 0;
                    if (a_beat_valid && b_beat_valid) begin
                        acc <= acc + sum_prods;
                        a_beat_buf <= a_beat_buf >> DATA_W;
                        b_beat_buf <= b_beat_buf >> DATA_W;
                        step_cnt <= 1;
                        a_beat_valid <= 0;
                        b_beat_valid <= 0;
                    end
                end

                WAIT_A: begin
                    step_cnt <= 0;
                    if (a_beat_valid) begin
                        acc <= acc + sum_prods;
                        a_beat_buf <= a_beat_buf >> DATA_W;
                        b_beat_buf <= b_beat_buf >> DATA_W;
                        step_cnt <= 1;
                        a_beat_valid <= 0;
                        b_beat_valid <= 0;
                    end
                end

                WAIT_B: begin
                    step_cnt <= 0;
                    if (b_beat_valid) begin
                        acc <= acc + sum_prods;
                        a_beat_buf <= a_beat_buf >> DATA_W;
                        b_beat_buf <= b_beat_buf >> DATA_W;
                        step_cnt <= 1;
                        a_beat_valid <= 0;
                        b_beat_valid <= 0;
                    end
                end

                PROC: begin
                    acc <= acc + sum_prods;
                    a_beat_buf <= a_beat_buf >> DATA_W;
                    b_beat_buf <= b_beat_buf >> DATA_W;
                    step_cnt <= step_cnt + 1;
                    if (step_cnt == STEPS-1) begin
                        beat_cnt <= beat_cnt - 1;
                        if (beat_cnt == 0) begin
                            out_reg <= acc + sum_prods;
                            out_valid_reg <= 1;
                            beat_cnt <= BEATS - 1;
                            acc <= 0;
                        end
                        step_cnt <= 0;
                    end
                end

                OUT: begin
                    if (out_ready) begin
                        out_valid_reg <= 0;
                    end
                end
            endcase
        end
    end
endmodule