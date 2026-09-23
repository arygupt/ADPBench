// ADPBench submission. Dot product module
// c = sum_i a[i] * b[i] for 256 int8 elements, output int32

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

    localparam BEATS = LEN / LANES;  // 8 beats per transaction
    localparam CNT_W = 4;  // Need to count 0-8

    // State
    reg [1:0] state;
    reg [CNT_W-1:0] beat_count;
    reg signed [ACC_W-1:0] accumulator;
    reg result_valid;

    // Handshakes
    wire a_fire = in_a_flat_valid & in_a_flat_ready;
    wire b_fire = in_b_flat_valid & in_b_flat_ready;
    wire all_fire = a_fire & b_fire;

    // Ready: active in IDLE, or in RUN while more beats expected
    assign in_a_flat_ready = (state == 2'd0) || (state == 2'd1 && beat_count < (BEATS[CNT_W-1:0] - 1));
    assign in_b_flat_ready = (state == 2'd0) || (state == 2'd1 && beat_count < (BEATS[CNT_W-1:0] - 1));

    // Compute all 32 lane products
    wire signed [2*DATA_W-1:0] p0  = $signed(in_a_flat[0*DATA_W +: DATA_W]) * $signed(in_b_flat[0*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p1  = $signed(in_a_flat[1*DATA_W +: DATA_W]) * $signed(in_b_flat[1*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p2  = $signed(in_a_flat[2*DATA_W +: DATA_W]) * $signed(in_b_flat[2*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p3  = $signed(in_a_flat[3*DATA_W +: DATA_W]) * $signed(in_b_flat[3*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p4  = $signed(in_a_flat[4*DATA_W +: DATA_W]) * $signed(in_b_flat[4*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p5  = $signed(in_a_flat[5*DATA_W +: DATA_W]) * $signed(in_b_flat[5*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p6  = $signed(in_a_flat[6*DATA_W +: DATA_W]) * $signed(in_b_flat[6*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p7  = $signed(in_a_flat[7*DATA_W +: DATA_W]) * $signed(in_b_flat[7*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p8  = $signed(in_a_flat[8*DATA_W +: DATA_W]) * $signed(in_b_flat[8*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p9  = $signed(in_a_flat[9*DATA_W +: DATA_W]) * $signed(in_b_flat[9*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p10 = $signed(in_a_flat[10*DATA_W +: DATA_W]) * $signed(in_b_flat[10*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p11 = $signed(in_a_flat[11*DATA_W +: DATA_W]) * $signed(in_b_flat[11*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p12 = $signed(in_a_flat[12*DATA_W +: DATA_W]) * $signed(in_b_flat[12*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p13 = $signed(in_a_flat[13*DATA_W +: DATA_W]) * $signed(in_b_flat[13*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p14 = $signed(in_a_flat[14*DATA_W +: DATA_W]) * $signed(in_b_flat[14*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p15 = $signed(in_a_flat[15*DATA_W +: DATA_W]) * $signed(in_b_flat[15*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p16 = $signed(in_a_flat[16*DATA_W +: DATA_W]) * $signed(in_b_flat[16*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p17 = $signed(in_a_flat[17*DATA_W +: DATA_W]) * $signed(in_b_flat[17*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p18 = $signed(in_a_flat[18*DATA_W +: DATA_W]) * $signed(in_b_flat[18*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p19 = $signed(in_a_flat[19*DATA_W +: DATA_W]) * $signed(in_b_flat[19*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p20 = $signed(in_a_flat[20*DATA_W +: DATA_W]) * $signed(in_b_flat[20*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p21 = $signed(in_a_flat[21*DATA_W +: DATA_W]) * $signed(in_b_flat[21*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p22 = $signed(in_a_flat[22*DATA_W +: DATA_W]) * $signed(in_b_flat[22*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p23 = $signed(in_a_flat[23*DATA_W +: DATA_W]) * $signed(in_b_flat[23*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p24 = $signed(in_a_flat[24*DATA_W +: DATA_W]) * $signed(in_b_flat[24*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p25 = $signed(in_a_flat[25*DATA_W +: DATA_W]) * $signed(in_b_flat[25*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p26 = $signed(in_a_flat[26*DATA_W +: DATA_W]) * $signed(in_b_flat[26*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p27 = $signed(in_a_flat[27*DATA_W +: DATA_W]) * $signed(in_b_flat[27*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p28 = $signed(in_a_flat[28*DATA_W +: DATA_W]) * $signed(in_b_flat[28*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p29 = $signed(in_a_flat[29*DATA_W +: DATA_W]) * $signed(in_b_flat[29*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p30 = $signed(in_a_flat[30*DATA_W +: DATA_W]) * $signed(in_b_flat[30*DATA_W +: DATA_W]);
    wire signed [2*DATA_W-1:0] p31 = $signed(in_a_flat[31*DATA_W +: DATA_W]) * $signed(in_b_flat[31*DATA_W +: DATA_W]);

    // Sum of all products in this beat
    wire signed [ACC_W-1:0] beat_products = p0 + p1 + p2 + p3 + p4 + p5 + p6 + p7 +
                                            p8 + p9 + p10 + p11 + p12 + p13 + p14 + p15 +
                                            p16 + p17 + p18 + p19 + p20 + p21 + p22 + p23 +
                                            p24 + p25 + p26 + p27 + p28 + p29 + p30 + p31;

    // FSM
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= 2'd0;  // IDLE
            beat_count <= 0;
            accumulator <= 0;
            result_valid <= 0;
        end else begin
            case (state)
                2'd0: begin  // IDLE
                    beat_count <= 0;
                    accumulator <= 0;
                    result_valid <= 0;
                    if (all_fire) begin
                        state <= 2'd1;  // RUN
                        beat_count <= 1;
                        accumulator <= beat_products;
                    end
                end

                2'd1: begin  // RUN
                    if (all_fire) begin
                        if (beat_count == BEATS - 1) begin
                            // Last beat - done
                            accumulator <= accumulator + beat_products;
                            state <= 2'd2;  // DONE
                            result_valid <= 1;
                        end else begin
                            accumulator <= accumulator + beat_products;
                            beat_count <= beat_count + 1;
                        end
                    end
                end

                2'd2: begin  // DONE
                    if (out_ready) begin
                        state <= 2'd0;  // IDLE
                        result_valid <= 0;
                    end
                end

                default: state <= 2'd0;
            endcase
        end
    end

    assign out_valid = result_valid;
    assign out_c = accumulator;

endmodule
