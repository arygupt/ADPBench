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

    localparam NUM_BEATS = LEN / LANES;

    localparam S_ACC  = 1'd0;
    localparam S_OUT  = 1'd1;
    reg state;

    reg signed [ACC_W-1:0] accumulator;
    reg [$clog2(NUM_BEATS):0] beat_cnt;  // extra bit for safety

    // Combinational beat sum using generate
    wire signed [ACC_W-1:0] prod [0:LANES-1];
    genvar i;
    generate
        for (i = 0; i < LANES; i = i + 1) begin : gen_prod
            assign prod[i] = $signed(in_a_flat[i*DATA_W +: DATA_W]) * $signed(in_b_flat[i*DATA_W +: DATA_W]);
        end
    endgenerate

    wire signed [ACC_W-1:0] sum_l1 [0:15];
    wire signed [ACC_W-1:0] sum_l2 [0:7];
    wire signed [ACC_W-1:0] sum_l3 [0:3];
    wire signed [ACC_W-1:0] sum_l4 [0:1];

    generate
        for (i = 0; i < 16; i = i + 1) begin : gl1
            assign sum_l1[i] = prod[2*i] + prod[2*i+1];
        end
        for (i = 0; i < 8; i = i + 1) begin : gl2
            assign sum_l2[i] = sum_l1[2*i] + sum_l1[2*i+1];
        end
        for (i = 0; i < 4; i = i + 1) begin : gl3
            assign sum_l3[i] = sum_l2[2*i] + sum_l2[2*i+1];
        end
        for (i = 0; i < 2; i = i + 1) begin : gl4
            assign sum_l4[i] = sum_l3[2*i] + sum_l3[2*i+1];
        end
    endgenerate

    wire signed [ACC_W-1:0] beat_sum = sum_l4[0] + sum_l4[1];
    wire signed [ACC_W-1:0] acc_next = accumulator + beat_sum;

    // Ready: accept inputs in S_ACC, output in S_OUT
    wire do_acc = (state == S_ACC) && in_a_flat_valid && in_b_flat_valid;
    assign in_a_flat_ready = (state == S_ACC);
    assign in_b_flat_ready = (state == S_ACC);
    assign out_c = accumulator;
    assign out_valid = (state == S_OUT);

    always_ff @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= S_ACC;
            beat_cnt <= '0;
            accumulator <= '0;
        end else begin
            case (state)
                S_ACC: begin
                    if (do_acc) begin
                        accumulator <= acc_next;
                        if (beat_cnt == NUM_BEATS - 1) begin
                            state <= S_OUT;
                            beat_cnt <= '0;
                        end else begin
                            beat_cnt <= beat_cnt + 1'b1;
                        end
                    end
                end
                S_OUT: begin
                    if (out_ready) begin
                        state <= S_ACC;
                        beat_cnt <= '0;
                        accumulator <= '0;
                    end
                end
            endcase
        end
    end

endmodule
