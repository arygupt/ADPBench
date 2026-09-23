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

    localparam NUM_BEATS = LEN / LANES; // 256/32 = 8
    localparam BEAT_LOG2 = 3; // log2(8)

    // Pipeline stages: 8 beats accumulate, then output.
    // We'll use a simple state machine.

    // State encoding
    localparam S_IDLE     = 2'd0;
    localparam S_ACCUM    = 2'd1;
    localparam S_OUTPUT   = 2'd2;

    reg [1:0] state, state_next;
    reg [BEAT_LOG2-1:0] beat_cnt, beat_cnt_next;
    reg signed [ACC_W-1:0] acc, acc_next;
    reg out_valid_reg;

    // Input handshake: accept only in S_ACCUM state
    assign in_a_flat_ready = (state == S_ACCUM);
    assign in_b_flat_ready = (state == S_ACCUM);

    // Output
    assign out_valid = out_valid_reg;
    assign out_c = acc;

    // State register
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_IDLE;
            beat_cnt <= 0;
            acc <= 0;
            out_valid_reg <= 0;
        end else begin
            state <= state_next;
            beat_cnt <= beat_cnt_next;
            acc <= acc_next;
            // Output valid is asserted in S_OUTPUT until accepted
            if (state == S_OUTPUT && out_ready)
                out_valid_reg <= 1'b0;
            else if (state_next == S_OUTPUT)
                out_valid_reg <= 1'b1;
        end
    end

    // Combinational next state logic
    always @(*) begin
        state_next = state;
        beat_cnt_next = beat_cnt;
        acc_next = acc;

        case (state)
            S_IDLE: begin
                state_next = S_ACCUM;
                beat_cnt_next = 0;
                acc_next = 0;
            end

            S_ACCUM: begin
                if (in_a_flat_valid && in_b_flat_valid) begin
                    // Compute partial sum for this beat
                    reg signed [ACC_W-1:0] partial_sum;
                    partial_sum = 0;
                    for (int i = 0; i < LANES; i = i + 1) begin
                        partial_sum = partial_sum +
                            $signed(in_a_flat[i*DATA_W +: DATA_W]) *
                            $signed(in_b_flat[i*DATA_W +: DATA_W]);
                    end
                    acc_next = acc + partial_sum;

                    if (beat_cnt == NUM_BEATS - 1) begin
                        state_next = S_OUTPUT;
                        beat_cnt_next = 0;
                    end else begin
                        beat_cnt_next = beat_cnt + 1;
                    end
                end
            end

            S_OUTPUT: begin
                if (out_ready) begin
                    state_next = S_IDLE;
                end
            end

            default: state_next = S_IDLE;
        endcase
    end

endmodule
